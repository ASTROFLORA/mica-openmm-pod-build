w"""WebSocket handler for real-time MD simulation event streaming (W4-1/W4-2).

Provides:
  1. An in-process pub/sub channel keyed by ``job_id``
  2. ``/ws/md/{job_id}`` — clients subscribe to MD lifecycle events
  3. ``publish_md_event()`` — callable from the ``on_event`` callback in
     the biodynamo driver to push orchestrator events into the WS channel

Architecture:
  - ``on_event(phase, msg, snapshot)`` in biodynamo_driver is synchronous.
    ``publish_md_event`` uses ``fire_and_forget`` to bridge sync → async.
  - Each connected client receives JSON frames:
    ``{"job_id": "...", "phase": "...", "message": "...", "snapshot": {...}, "ts": "..."}``
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

# ── In-process pub/sub ──────────────────────────────────────────

# job_id → set of asyncio.Queue (one per connected WS client)
_subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
_subscriber_lock = asyncio.Lock()
_job_owners: Dict[str, str] = {}
_stream_metrics: Dict[str, Dict[str, Any]] = defaultdict(dict)
_event_sequences: Dict[str, int] = defaultdict(int)

COMMON_MD_EVENT_IDENTITY_FIELDS = (
    "event_type",
    "job_id",
    "route_decision_id",
    "provider",
    "provider_instance_id",
    "requested_gpu_type",
    "actual_gpu_type",
    "gcs_prefix",
    "image_digest",
    "source_target_id",
    "timestamp",
    "sequence_id",
    "allocation_attempt",
    "protocol_node_id",
    "session_id",
)

MAX_TRAJECTORY_INLINE_BYTES = int(os.getenv("MICA_WS_TRAJECTORY_INLINE_MAX_BYTES", "65536"))


def _is_production_env() -> bool:
    env = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "development"
    return str(env).lower() in ("prod", "production")


def register_md_job_owner(job_id: str, user_id: str) -> None:
    if job_id and user_id:
        _job_owners[str(job_id)] = str(user_id)


def _authenticate_websocket(websocket: WebSocket) -> str:
    authorization = websocket.headers.get("authorization")
    token = websocket.query_params.get("token")
    ticket = websocket.query_params.get("ticket")
    if not authorization and token:
        authorization = f"Bearer {token}"
    if not authorization and ticket:
        authorization = f"Bearer {ticket}"

    allow_user_fallback = (os.getenv("MICA_WS_ALLOW_USER_ID_FALLBACK") or "false").lower() == "true"
    x_user_id = None
    if allow_user_fallback:
        x_user_id = websocket.headers.get("x-user-id") or websocket.query_params.get("user_id")

    return user_dependency(x_user_id=x_user_id, authorization=authorization)


async def _redis_job_owner(job_id: str) -> Optional[str]:
    try:
        from mica.infrastructure.redis_client import get_redis_if_configured
        from mica.worker.job_store import RedisJobStore

        redis_client = await get_redis_if_configured(decode_responses=False, verify_connection=True)
        if redis_client is None:
            return None
        record = await RedisJobStore(redis_client).get(job_id)
        if record is None:
            return None
        return str(record.get("user_id") or "") or None
    except Exception as exc:
        logger.warning("MD WS owner lookup failed for job %s: %s", job_id, exc)
        return None


async def _authorize_job_subscription(job_id: str, user_id: str) -> bool:
    owner = _job_owners.get(str(job_id))
    if owner is None:
        owner = await _redis_job_owner(job_id)
    if owner:
        return owner == user_id
    return not _is_production_env()


async def _subscribe(job_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    async with _subscriber_lock:
        _subscribers[job_id].add(q)
    return q


async def _unsubscribe(job_id: str, q: asyncio.Queue) -> None:
    async with _subscriber_lock:
        _subscribers[job_id].discard(q)
        if not _subscribers[job_id]:
            del _subscribers[job_id]
    # Keep metrics after disconnect for post-mortem visibility.
    m = _stream_metrics.setdefault(job_id, {})
    m["subscriber_count"] = len(_subscribers.get(job_id, set()))


async def _broadcast(job_id: str, payload: dict) -> None:
    async with _subscriber_lock:
        queues = list(_subscribers.get(job_id, set()))
    m = _stream_metrics.setdefault(job_id, {})
    m["published"] = int(m.get("published", 0)) + 1
    m["last_event_ts"] = payload.get("ts")
    m["last_event_type"] = payload.get("type", "md_event")
    m["subscriber_count"] = len(queues)
    dropped = 0
    for q in queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dropped += 1
            logger.warning("MD WS queue full for job %s — dropping event", job_id)
    if dropped:
        m["dropped"] = int(m.get("dropped", 0)) + dropped
        m["last_drop_ts"] = datetime.now(timezone.utc).isoformat()


def get_md_stream_metrics(job_id: str) -> Dict[str, Any]:
    """Return lightweight per-job stream metrics for observability tooling."""
    m = _stream_metrics.get(str(job_id), {})
    return {
        "job_id": str(job_id),
        "published": int(m.get("published", 0)),
        "dropped": int(m.get("dropped", 0)),
        "last_event_type": m.get("last_event_type"),
        "last_event_ts": m.get("last_event_ts"),
        "last_drop_ts": m.get("last_drop_ts"),
        "subscriber_count": int(m.get("subscriber_count", 0)),
    }


def validate_md_event_identity_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a validation summary for common MD stream identity fields."""
    missing = [field for field in COMMON_MD_EVENT_IDENTITY_FIELDS if field not in payload]
    return {
        "valid": not missing,
        "missing": missing,
        "required_fields": list(COMMON_MD_EVENT_IDENTITY_FIELDS),
    }


def _next_sequence_id(job_id: str) -> int:
    _event_sequences[str(job_id)] += 1
    return _event_sequences[str(job_id)]


def _normalize_md_event_payload(
    job_id: str,
    payload: Dict[str, Any],
    *,
    event_type: Optional[str] = None,
    sequence_id: Optional[int] = None,
) -> Dict[str, Any]:
    normalized = dict(payload)
    metadata = normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {}
    event_name = str(event_type or normalized.get("event_type") or normalized.get("type") or "md_event").strip() or "md_event"
    timestamp = str(normalized.get("timestamp") or normalized.get("ts") or datetime.now(timezone.utc).isoformat())

    normalized["job_id"] = str(normalized.get("job_id") or job_id)
    normalized["type"] = event_name
    normalized["event_type"] = event_name
    normalized["timestamp"] = timestamp
    normalized["ts"] = timestamp

    field_defaults = {
        "route_decision_id": metadata.get("route_decision_id", ""),
        "provider": metadata.get("provider", ""),
        "provider_instance_id": metadata.get("provider_instance_id") or metadata.get("source_container_group") or "",
        "requested_gpu_type": metadata.get("requested_gpu_type", ""),
        "actual_gpu_type": metadata.get("actual_gpu_type", ""),
        "gcs_prefix": metadata.get("gcs_prefix") or metadata.get("output_gcs_prefix") or "",
        "image_digest": metadata.get("image_digest", ""),
        "source_target_id": metadata.get("source_target_id", ""),
        "allocation_attempt": metadata.get("allocation_attempt", 0),
        "protocol_node_id": metadata.get("protocol_node_id", ""),
        "session_id": metadata.get("session_id", ""),
    }
    for field, default_value in field_defaults.items():
        normalized.setdefault(field, default_value)

    if sequence_id is None:
        raw_sequence = normalized.get("sequence_id") or metadata.get("sequence_id")
        try:
            sequence_id = int(raw_sequence)
        except (TypeError, ValueError):
            sequence_id = _next_sequence_id(str(normalized["job_id"]))
    normalized["sequence_id"] = int(sequence_id)

    try:
        normalized["allocation_attempt"] = int(normalized.get("allocation_attempt") or 0)
    except (TypeError, ValueError):
        normalized["allocation_attempt"] = 0

    normalized["identity_validation"] = validate_md_event_identity_fields(normalized)
    return normalized


def _bounded_inline_payload(payload_inline: str) -> tuple[str, int, bool]:
    encoded = str(payload_inline or "").encode("utf-8")
    size = len(encoded)
    if size > MAX_TRAJECTORY_INLINE_BYTES:
        return "", size, True
    return str(payload_inline or ""), size, False


# ── Public publish API (called from biodynamo driver) ────────────

_loop_ref: Optional[asyncio.AbstractEventLoop] = None


def _dispatch_md_payload(job_id: str, payload: Dict[str, Any]) -> None:
    """Broadcast a typed MD payload on the shared per-job stream."""
    payload = _normalize_md_event_payload(job_id, payload)
    loop = _loop_ref
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No event loop for MD WS publish — event dropped")
            return

    if loop.is_running():
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            loop.create_task(_broadcast(job_id, payload))
        else:
            loop.call_soon_threadsafe(loop.create_task, _broadcast(job_id, payload))
    else:
        logger.debug("Event loop not running — MD WS event dropped")


def publish_md_event(
    job_id: str,
    phase: str,
    message: str,
    snapshot: Optional[Dict[str, Any]] = None,
) -> None:
    """Publish an MD orchestrator event into the WS channel.

    Safe to call from synchronous ``on_event`` callbacks — it schedules
    the broadcast coroutine on the running event loop.
    """
    payload = {
        "job_id": job_id,
        "phase": phase,
        "message": message,
        "snapshot": snapshot or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    _dispatch_md_payload(job_id, payload)


def publish_md_progress_event(
    job_id: str,
    *,
    route_decision_id: str,
    provider: str,
    provider_instance_id: str,
    gcs_prefix: str,
    progress_percent: Optional[float] = None,
    simulated_ns: Optional[float] = None,
    step: Optional[int] = None,
    time_ps: Optional[float] = None,
    cadence_policy: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
) -> None:
    payload = {
        "job_id": job_id,
        "type": "md_progress",
        "event_type": "md_progress",
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "progress_percent": progress_percent,
        "simulated_ns": simulated_ns,
        "step": step,
        "time_ps": time_ps,
        "cadence_policy": dict(cadence_policy or {}),
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)


def publish_compute_status_event(
    job_id: str,
    *,
    route_decision_id: str,
    provider: str,
    provider_instance_id: str,
    gcs_prefix: str,
    status: str,
    artifact_ref: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
) -> None:
    payload = {
        "job_id": job_id,
        "type": "compute_status",
        "event_type": "compute_status",
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "status": status,
        "artifact_ref": artifact_ref,
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)


def publish_artifact_transmission_event(
    job_id: str,
    *,
    route_decision_id: str,
    provider: str,
    provider_instance_id: str,
    gcs_prefix: str,
    artifact_ref: str,
    object_uri: str = "",
    sha256: str = "",
    size_bytes: Optional[int] = None,
    content_type: str = "",
    durability_class: str = "",
    readback_verified: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
) -> None:
    payload = {
        "job_id": job_id,
        "type": "artifact_transmission",
        "event_type": "artifact_transmission",
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "artifact_ref": artifact_ref,
        "object_uri": object_uri or artifact_ref,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "durability_class": durability_class,
        "readback_verified": bool(readback_verified),
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)


def publish_worker_heartbeat_event(
    job_id: str,
    *,
    route_decision_id: str,
    provider: str,
    provider_instance_id: str,
    gcs_prefix: str,
    status: str = "running",
    metadata: Optional[Dict[str, Any]] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
) -> None:
    payload = {
        "job_id": job_id,
        "type": "worker_heartbeat",
        "event_type": "worker_heartbeat",
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "status": status,
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)


def publish_checkpoint_written_event(
    job_id: str,
    *,
    route_decision_id: str,
    provider: str,
    provider_instance_id: str,
    gcs_prefix: str,
    checkpoint_ref: str,
    metadata: Optional[Dict[str, Any]] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
) -> None:
    payload = {
        "job_id": job_id,
        "type": "checkpoint_written",
        "event_type": "checkpoint_written",
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "checkpoint_ref": checkpoint_ref,
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)


def publish_terminal_status_event(
    job_id: str,
    *,
    route_decision_id: str,
    provider: str,
    provider_instance_id: str,
    gcs_prefix: str,
    status: str,
    evidencegate_status: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
) -> None:
    event_name = "evidencegate_status" if evidencegate_status else "terminal_status"
    payload = {
        "job_id": job_id,
        "type": event_name,
        "event_type": event_name,
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "status": status,
        "evidencegate_status": evidencegate_status,
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)


def publish_trajectory_frame(
    job_id: str,
    frame_index: int,
    step: int,
    time_ps: float,
    pdb_data: str,
    run_id: Optional[str] = None,
    route_decision_id: str = "",
    provider: str = "",
    provider_instance_id: str = "",
    gcs_prefix: str = "",
    payload_ref: str = "",
    source_artifact_ref: str = "",
    event_format: str = "pdb_preview",
    bcif_preview_status: str = "degraded_or_not_implemented",
    fallback_event_format: str = "pdb_preview",
    preview_payload_format: str = "pdb",
    pdb_preview_ref: str = "",
    bcif_preview_ref: str = "",
    mmcif_preview_ref: str = "",
    preview_encoder: str = "",
    preview_encoder_error: str = "",
    preview_not_canonical: bool = True,
    size_bytes: Optional[int] = None,
    sha256: str = "",
    content_type: str = "",
    readback_verified: bool = False,
    source_topology_ref: str = "",
    source_positions_ref: str = "",
    source_trajectory_ref: str = "",
    worker_produced_at: str = "",
    gcs_observed_at: str = "",
    requested_frame_interval_ps: Optional[float] = None,
    actual_frame_interval_ps: Optional[float] = None,
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
    durability_class: str = "stream-preview",
) -> None:
    """Publish a trajectory frame into the WS channel for Molstar preview.

    Like ``publish_md_event`` but with a dedicated ``trajectory_frame`` type
    and structured fields that Molstar/NGL clients can consume directly.
    """
    payload_inline, payload_size_bytes, payload_truncated = _bounded_inline_payload(pdb_data)
    resolved_size_bytes = int(size_bytes) if size_bytes is not None else payload_size_bytes
    payload = {
        "job_id": job_id,
        "type": "trajectory_frame",
        "event_type": "trajectory_frame",
        "run_id": run_id or job_id,
        "frame_index": frame_index,
        "step": step,
        "time_ps": time_ps,
        "format": event_format,
        "data": payload_inline,
        "payload_inline": payload_inline,
        "payload_inline_truncated": payload_truncated,
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "preview_not_canonical": bool(preview_not_canonical),
        "payload_ref": payload_ref,
        "source_artifact_ref": source_artifact_ref or payload_ref,
        "bcif_preview_status": bcif_preview_status,
        "fallback_event_format": fallback_event_format,
        "preview_payload_format": preview_payload_format,
        "pdb_preview_ref": pdb_preview_ref,
        "bcif_preview_ref": bcif_preview_ref,
        "mmcif_preview_ref": mmcif_preview_ref,
        "preview_encoder": preview_encoder,
        "preview_encoder_error": preview_encoder_error,
        "requested_frame_interval_ps": requested_frame_interval_ps,
        "actual_frame_interval_ps": actual_frame_interval_ps,
        "payload_size_bytes": resolved_size_bytes,
        "content_type": content_type,
        "readback_verified": bool(readback_verified),
        "source_topology_ref": source_topology_ref,
        "source_positions_ref": source_positions_ref,
        "source_trajectory_ref": source_trajectory_ref,
        "worker_produced_at": worker_produced_at,
        "gcs_observed_at": gcs_observed_at,
        "durability_class": durability_class,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if provider_instance_id:
        payload["provider_container_group"] = provider_instance_id
    payload["size_bytes"] = resolved_size_bytes
    if sha256:
        payload["sha256"] = sha256

    _dispatch_md_payload(job_id, payload)


def publish_smic_metric_event(
    job_id: str,
    *,
    metric_key: str,
    value: Optional[float] = None,
    unit: str,
    metric_status: str = "completed",
    value_ref: str = "",
    frame_index: Optional[int] = None,
    time_ps: Optional[float] = None,
    window_start_ps: Optional[float] = None,
    window_end_ps: Optional[float] = None,
    source_topology_ref: str = "",
    source_trajectory_ref: str = "",
    topology_atoms: Optional[int] = None,
    trajectory_atoms: Optional[int] = None,
    output_artifact_refs: Optional[list[str]] = None,
    failure_code: str = "",
    route_decision_id: str = "",
    provider: str = "",
    provider_instance_id: str = "",
    gcs_prefix: str = "",
    requested_gpu_type: str = "",
    actual_gpu_type: str = "",
    image_digest: str = "",
    source_target_id: str = "",
    sequence_id: Optional[int] = None,
    allocation_attempt: int = 0,
    protocol_node_id: str = "",
    session_id: str = "",
    run_id: Optional[str] = None,
    series_point: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Publish a live SMIC metric sample on the shared MD stream."""
    payload = {
        "job_id": job_id,
        "type": "smic_metric",
        "event_type": "smic_metric",
        "run_id": run_id or job_id,
        "metric_key": metric_key,
        "metric_name": metric_key,
        "metric_status": metric_status,
        "value": value,
        "value_ref": value_ref,
        "unit": unit,
        "frame_index": frame_index,
        "time_ps": time_ps,
        "window_start_ps": window_start_ps,
        "window_end_ps": window_end_ps,
        "source_topology_ref": source_topology_ref,
        "source_trajectory_ref": source_trajectory_ref,
        "topology_atoms": topology_atoms,
        "trajectory_atoms": trajectory_atoms,
        "output_artifact_refs": list(output_artifact_refs or []),
        "failure_code": failure_code,
        "no_fake_metric": True,
        "route_decision_id": route_decision_id,
        "provider": provider,
        "provider_instance_id": provider_instance_id,
        "gcs_prefix": gcs_prefix,
        "requested_gpu_type": requested_gpu_type,
        "actual_gpu_type": actual_gpu_type,
        "image_digest": image_digest,
        "source_target_id": source_target_id,
        "sequence_id": sequence_id,
        "allocation_attempt": allocation_attempt,
        "protocol_node_id": protocol_node_id,
        "session_id": session_id,
        "series_point": dict(series_point or {}),
        "metadata": dict(metadata or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _dispatch_md_payload(job_id, payload)



# ── WebSocket handler ────────────────────────────────────────────

async def handle_md_websocket(websocket: WebSocket, job_id: str) -> None:
    """Handle a single ``/ws/md/{job_id}`` connection.

    Protocol:
      1. Accept the connection
      2. Subscribe to the job's event stream
      3. Stream events as JSON frames
      4. On disconnect or error, unsubscribe and close
    """
    global _loop_ref
    _loop_ref = asyncio.get_running_loop()

    try:
        user_id = _authenticate_websocket(websocket)
    except HTTPException as exc:
        logger.warning("MD WS rejected unauthenticated connection job_id=%s status=%s", job_id, exc.status_code)
        await websocket.close(code=1008)
        return

    if not await _authorize_job_subscription(job_id, user_id):
        logger.warning("MD WS rejected unauthorized subscription job_id=%s user_id=%s", job_id, user_id)
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info("MD WS connected: job_id=%s user_id=%s", job_id, user_id)

    q = await _subscribe(job_id)
    try:
        # Send initial ack so the client knows the subscription is active
        await websocket.send_json({
            "type": "subscribed",
            "job_id": job_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        while True:
            # Use a 30s timeout so we can send keep-alive pings
            try:
                payload = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a ping/keep-alive
                await websocket.send_json({
                    "type": "ping",
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                continue

            # Preserve typed payloads (e.g., trajectory_frame), fallback to md_event.
            payload.setdefault("type", "md_event")
            await websocket.send_json(payload)

    except WebSocketDisconnect:
        logger.info("MD WS disconnected: job_id=%s", job_id)
    except Exception as exc:
        logger.error("MD WS error for job_id=%s: %s", job_id, exc)
    finally:
        await _unsubscribe(job_id, q)
        try:
            await websocket.close()
        except Exception:
            pass
