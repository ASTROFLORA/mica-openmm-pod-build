from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from mica.infrastructure.orchestration.sp04_durability_truth import build_sp04_packet
from mica.serverless_models.execution_records import project_remote_md_session_to_execution_record
from .remote_md_session_sidecar import redact_sensitive_payload


_KEY_PREFIX = "mica:md"
_ACTIVE_KEY = f"{_KEY_PREFIX}:active"


def default_remote_md_registry_path() -> str:
    configured = str(os.getenv("MICA_MD_SESSION_REGISTRY_FILE", "")).strip()
    if configured:
        return str(Path(configured).expanduser().resolve())
    return str((Path.home() / ".mica" / "remote_md_session_registry.json").resolve())


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _session_key(session_id: str) -> str:
    return f"{_KEY_PREFIX}:sessions:{session_id}"


def _job_key(job_id: str) -> str:
    return f"{_KEY_PREFIX}:job:{job_id}"


def _instance_key(instance_id: str) -> str:
    return f"{_KEY_PREFIX}:instance:{instance_id}"


def _user_key(user_id: str) -> str:
    return f"{_KEY_PREFIX}:user:{user_id}"


def _events_key(session_id: str) -> str:
    return f"{_KEY_PREFIX}:events:{session_id}"


def _status_is_terminal(status: str) -> bool:
    return str(status or "").lower() in {
        "completed",
        "failed",
        "failed_recoverable",
        "error",
        "interrupted",
        "lost",
        "cancelled",
    }


def _is_expired(lease_until: str) -> bool:
    candidate = str(lease_until or "").strip()
    if not candidate:
        return True
    try:
        normalized = candidate.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp() <= time.time()
    except ValueError:
        return True


def _sp04_event_id(kind: str, *parts: Any) -> str:
    seed = "|".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(f"{kind}|{seed}".encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


def _append_resource(resources: list[str], seen: set[str], prefix: str, value: Any) -> None:
    candidate = str(value or "").strip()
    if not candidate:
        return
    token = candidate if ":" in candidate else f"{prefix}:{candidate}"
    if token in seen:
        return
    seen.add(token)
    resources.append(token)


def _sp04_output_dir(session: Dict[str, Any]) -> str:
    for key in ("output_json", "artifact_manifest_path"):
        candidate = str(session.get(key) or "").strip()
        if not candidate or "://" in candidate:
            continue
        try:
            return str(Path(candidate).expanduser().resolve().parent)
        except OSError:
            continue
    return ""


def build_sp04_runtime_packet(session: Dict[str, Any]) -> Dict[str, Any]:
    execution_record = project_remote_md_session_to_execution_record(session)
    teardown_proof = dict(execution_record.get("teardown_proof") or {})
    session_id = str(session.get("session_id") or execution_record.get("session_id") or "").strip()
    job_id = str(session.get("job_id") or execution_record.get("provider_job_id") or session_id).strip()
    terminal_state = str(session.get("status") or execution_record.get("state") or "completed").strip().lower()
    finished_at = str(session.get("finished_at") or session.get("updated_at") or "").strip()
    provider_job_id = str(
        session.get("instance_id")
        or execution_record.get("provider_target")
        or execution_record.get("provider_job_id")
        or job_id
    ).strip()

    resources_targeted: list[str] = []
    seen_resources: set[str] = set()
    _append_resource(resources_targeted, seen_resources, "instance", teardown_proof.get("instance_id"))
    _append_resource(resources_targeted, seen_resources, "instance", session.get("instance_id"))
    _append_resource(resources_targeted, seen_resources, "provider_job", execution_record.get("provider_job_id"))

    teardown_state = str(teardown_proof.get("teardown_state") or "").strip().lower()
    destroy_attempted = bool(teardown_proof.get("destroy_attempted"))
    destroy_succeeded = bool(teardown_proof.get("destroy_succeeded"))
    orphan_scan_result = str(teardown_proof.get("orphan_scan_result") or "").strip().lower()
    preserved_for_recovery = bool(teardown_proof.get("preserved_for_recovery")) or terminal_state == "failed_recoverable" or not resources_targeted
    orphan_result = "none"
    if orphan_scan_result in {"detected", "orphaned", "orphans_detected"}:
        orphan_result = "detected"
    elif terminal_state in {"lost", "orphaned"}:
        orphan_result = "detected"
    elif teardown_state in {"unknown", "not_collected"}:
        orphan_result = "detected"
    elif not destroy_attempted and not destroy_succeeded:
        orphan_result = "detected"

    return build_sp04_packet(
        session_id=session_id,
        job_id=job_id,
        provider_job_id=provider_job_id,
        terminal_state=terminal_state,
        terminal_event_id=_sp04_event_id("terminal", session_id, job_id, terminal_state, finished_at),
        artifacts=list(execution_record.get("artifact_uris") or []),
        resources_targeted=resources_targeted,
        orphan_result=orphan_result,
        success=terminal_state == "completed",
        error=str(session.get("error") or "").strip(),
        durable=bool(session.get("output_json") or session.get("artifact_manifest_path") or execution_record.get("artifact_uris")),
        destroy_succeeded=destroy_succeeded,
        preserved_for_recovery=preserved_for_recovery,
        output_dir=_sp04_output_dir(session),
        durability_event_id=_sp04_event_id(
            "durability",
            session_id,
            job_id,
            finished_at,
            session.get("output_json") or session.get("artifact_manifest_path"),
        ),
    )


class RemoteMDSessionRegistry:
    def __init__(
        self,
        *,
        redis_client: Any = None,
        registry_path: str = "",
    ) -> None:
        self._redis = redis_client
        self._registry_path = str(registry_path or default_remote_md_registry_path())
        self._lock = asyncio.Lock()

    @property
    def registry_path(self) -> str:
        return self._registry_path

    async def create_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        existing = await self.get_session(session_id)
        merged = dict(existing or {})
        merged.update(deepcopy(payload))
        merged.setdefault("created_at", _utcnow())
        merged["updated_at"] = _utcnow()
        await self._save_session(merged)
        return merged

    async def update_session(self, session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = await self.get_session(session_id)
        if current is None:
            current = {"session_id": session_id, "created_at": _utcnow()}
        incoming = deepcopy(patch)
        current_status = str(current.get("status") or "").lower()
        incoming_status = str(incoming.get("status") or "").lower()
        if _status_is_terminal(current_status) and incoming_status and incoming_status != current_status:
            incoming["status"] = current_status
            incoming.setdefault("terminal_status_locked", True)
            incoming.setdefault("terminal_status_locked_at", _utcnow())
            incoming.setdefault(
                "terminal_status_locked_reason",
                f"ignored non-terminal patch status={incoming_status} after terminal status={current_status}",
            )
        current.update(incoming)
        current["session_id"] = session_id
        current["updated_at"] = _utcnow()
        await self._save_session(current)
        return current

    async def heartbeat(self, session_id: str, ts: str = "") -> Dict[str, Any]:
        timestamp = ts or _utcnow()
        return await self.update_session(
            session_id,
            {
                "last_heartbeat_at": timestamp,
                "updated_at": timestamp,
            },
        )

    async def append_event(self, session_id: str, event: Dict[str, Any]) -> None:
        event_payload = dict(event)
        event_payload.setdefault("ts", _utcnow())
        if self._redis is not None:
            await self._redis.rpush(_events_key(session_id), json.dumps(event_payload))
            await self._redis.ltrim(_events_key(session_id), -200, -1)
            return

        async with self._lock:
            data = await self._read_file_store()
            session = data["sessions"].setdefault(session_id, {"session_id": session_id, "created_at": _utcnow()})
            events = list(session.get("events") or [])
            events.append(event_payload)
            session["events"] = events[-200:]
            session["updated_at"] = _utcnow()
            data["sessions"][session_id] = session
            await self._write_file_store(data)

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        if self._redis is not None:
            raw = await self._redis.get(_session_key(session_id))
            return json.loads(raw) if raw else None
        data = await self._read_file_store()
        session = data["sessions"].get(session_id)
        return deepcopy(session) if session else None

    async def list_sessions(self) -> list[Dict[str, Any]]:
        if self._redis is not None:
            session_ids = await self._redis.zrevrange(_ACTIVE_KEY, 0, 1000)
            sessions = []
            for session_id in session_ids:
                loaded = await self.get_session(str(session_id))
                if loaded is not None:
                    sessions.append(loaded)
            return sessions
        data = await self._read_file_store()
        return [deepcopy(item) for item in data["sessions"].values()]

    async def list_active_sessions(self) -> list[Dict[str, Any]]:
        sessions = await self.list_sessions()
        return [item for item in sessions if not _status_is_terminal(str(item.get("status") or ""))]

    async def get_by_job_id(self, job_id: str) -> Optional[Dict[str, Any]]:
        candidate = str(job_id or "").strip()
        if not candidate:
            return None
        if self._redis is not None:
            session_id = await self._redis.get(_job_key(candidate))
            return await self.get_session(session_id) if session_id else None
        data = await self._read_file_store()
        session_id = data["job_index"].get(candidate)
        session = data["sessions"].get(session_id or "") if session_id else None
        return deepcopy(session) if session else None

    async def get_by_instance_id(self, instance_id: str) -> Optional[Dict[str, Any]]:
        candidate = str(instance_id or "").strip()
        if not candidate:
            return None
        if self._redis is not None:
            session_id = await self._redis.get(_instance_key(candidate))
            return await self.get_session(session_id) if session_id else None
        data = await self._read_file_store()
        session_id = data["instance_index"].get(candidate)
        session = data["sessions"].get(session_id or "") if session_id else None
        return deepcopy(session) if session else None

    async def resolve_session(self, identifier: str) -> Optional[Dict[str, Any]]:
        candidate = str(identifier or "").strip()
        if not candidate:
            return None
        direct = await self.get_session(candidate)
        if direct is not None:
            return direct
        by_job = await self.get_by_job_id(candidate)
        if by_job is not None:
            return by_job
        return await self.get_by_instance_id(candidate)

    async def claim_reattach(
        self,
        session_id: str,
        *,
        attached_by: str,
        lease_until: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        session = await self.get_session(session_id)
        if session is None:
            raise KeyError(f"unknown session_id={session_id}")
        current_owner = str(session.get("attached_by") or "").strip()
        current_lease = str(session.get("lease_until") or "").strip()
        if current_owner and current_owner != attached_by and not _is_expired(current_lease) and not force:
            raise RuntimeError(f"session {session_id} is already attached by {current_owner}")
        return await self.update_session(
            session_id,
            {
                "attached_by": attached_by,
                "lease_until": lease_until,
                "last_heartbeat_at": _utcnow(),
            },
        )

    async def release_reattach(self, session_id: str, *, attached_by: str) -> Dict[str, Any]:
        session = await self.get_session(session_id)
        if session is None:
            raise KeyError(f"unknown session_id={session_id}")
        current_owner = str(session.get("attached_by") or "").strip()
        if current_owner and current_owner != attached_by:
            return session
        return await self.update_session(
            session_id,
            {
                "attached_by": "",
                "lease_until": "",
            },
        )

    async def mark_completed(self, session_id: str, terminal_payload: Dict[str, Any]) -> Dict[str, Any]:
        current = await self.get_session(session_id) or {"session_id": session_id}
        patch = dict(terminal_payload)
        patch.setdefault("status", "completed")
        patch.setdefault("finished_at", _utcnow())
        # SP-07: All terminal records must carry a teardown_proof dict.
        # If the orchestrator did not emit one (e.g. local/mock runs), synthesize
        # a minimal skeleton so downstream result synthesis never sees a missing field.
        if not isinstance(patch.get("teardown_proof"), dict):
            patch["teardown_proof"] = {
                "teardown_state": "unknown",
                "destroy_attempted": False,
                "destroy_succeeded": False,
                "destroy_skipped_reason": "teardown_proof_not_collected_before_completion",
            }
        sp04_source = dict(current)
        sp04_source.update(patch)
        sp04_source["session_id"] = session_id
        try:
            sp04_packet = build_sp04_runtime_packet(sp04_source)
            patch["sp04_packet"] = sp04_packet
            patch["sp04_gate_verdict"] = dict(sp04_packet.get("gate_verdict") or {})
        except Exception as exc:
            message = f"sp04_runtime_projection_failed: {exc}"
            patch["sp04_gate_verdict"] = {
                "passed": False,
                "reason": message,
                "blockers": [message],
            }
        return await self.update_session(session_id, patch)

    async def mark_orphaned(self, session_id: str, reason: str = "launcher_heartbeat_expired") -> Dict[str, Any]:
        return await self.update_session(
            session_id,
            {
                "status": "orphaned",
                "orphaned": True,
                "orphaned_reason": reason,
                "orphaned_at": _utcnow(),
            },
        )

    async def mark_failed_recoverable(self, session_id: str, reason: str) -> Dict[str, Any]:
        return await self.update_session(
            session_id,
            {
                "status": "failed_recoverable",
                "recoverable": True,
                "error": reason,
                "finished_at": _utcnow(),
            },
        )

    async def mark_lost(self, session_id: str, reason: str) -> Dict[str, Any]:
        return await self.update_session(
            session_id,
            {
                "status": "lost",
                "error": reason,
                "finished_at": _utcnow(),
            },
        )

    def project_session_to_execution_record(self, session: Dict[str, Any]) -> Dict[str, Any]:
        return project_remote_md_session_to_execution_record(session)

    async def get_execution_record(self, identifier: str) -> Optional[Dict[str, Any]]:
        session = await self.resolve_session(identifier)
        if session is None:
            return None
        return self.project_session_to_execution_record(session)

    async def _save_session(self, session: Dict[str, Any]) -> None:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        safe_session = redact_sensitive_payload(session)
        safe_session["session_id"] = session_id
        safe_session["updated_at"] = _utcnow()
        if self._redis is not None:
            payload = json.dumps(safe_session, ensure_ascii=False, default=str)
            pipe = self._redis.pipeline(transaction=True)
            pipe.set(_session_key(session_id), payload)
            pipe.zadd(_ACTIVE_KEY, {session_id: time.time()})
            user_id = str(safe_session.get("user_id") or "").strip()
            if user_id:
                pipe.sadd(_user_key(user_id), session_id)
            job_id = str(safe_session.get("job_id") or "").strip()
            if job_id:
                pipe.set(_job_key(job_id), session_id)
            instance_id = str(safe_session.get("instance_id") or "").strip()
            if instance_id:
                pipe.set(_instance_key(instance_id), session_id)
            if _status_is_terminal(str(safe_session.get("status") or "")):
                pipe.zrem(_ACTIVE_KEY, session_id)
            await pipe.execute()
            return

        async with self._lock:
            data = await self._read_file_store()
            previous = data["sessions"].get(session_id, {})
            previous_job_id = str(previous.get("job_id") or "").strip()
            previous_instance_id = str(previous.get("instance_id") or "").strip()
            if previous_job_id and data["job_index"].get(previous_job_id) == session_id:
                del data["job_index"][previous_job_id]
            if previous_instance_id and data["instance_index"].get(previous_instance_id) == session_id:
                del data["instance_index"][previous_instance_id]
            data["sessions"][session_id] = deepcopy(safe_session)
            user_id = str(safe_session.get("user_id") or "").strip()
            if user_id:
                entries = set(data["user_index"].get(user_id) or [])
                entries.add(session_id)
                data["user_index"][user_id] = sorted(entries)
            job_id = str(safe_session.get("job_id") or "").strip()
            if job_id:
                data["job_index"][job_id] = session_id
            instance_id = str(safe_session.get("instance_id") or "").strip()
            if instance_id:
                data["instance_index"][instance_id] = session_id
            await self._write_file_store(data)

    async def _read_file_store(self) -> Dict[str, Any]:
        target = Path(self._registry_path)
        if not target.exists():
            return {
                "sessions": {},
                "job_index": {},
                "instance_index": {},
                "user_index": {},
            }
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return {
                "sessions": {},
                "job_index": {},
                "instance_index": {},
                "user_index": {},
            }

    async def _write_file_store(self, data: Dict[str, Any]) -> None:
        target = Path(self._registry_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


async def create_default_remote_md_session_registry(registry_path: str = "") -> RemoteMDSessionRegistry:
    explicit_registry_path = str(registry_path or "").strip()
    if explicit_registry_path:
        return RemoteMDSessionRegistry(registry_path=explicit_registry_path)
    try:
        from mica.infrastructure.redis_client import get_redis

        redis_client = await get_redis()
        return RemoteMDSessionRegistry(redis_client=redis_client, registry_path=registry_path)
    except Exception:
        return RemoteMDSessionRegistry(registry_path=registry_path)