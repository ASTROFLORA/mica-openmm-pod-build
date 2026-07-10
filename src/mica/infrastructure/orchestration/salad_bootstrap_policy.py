"""Progress-aware Salad bootstrap/status policy for BioDynamo Vol2.

The policy is grounded in the local Salad docs mirror:
`.mica/external_docs/salad/container-engine/explanation/container-groups/deployment-lifecycle.mdx`,
`.mica/external_docs/salad/container-engine/explanation/container-groups/system-events.mdx`, and
`.mica/external_docs/salad/container-engine/explanation/container-groups/container-groups.mdx`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


INTERNAL_BOOTSTRAP_STATES = (
    "submit_accepted",
    "container_group_created",
    "allocating",
    "instance_allocated",
    "downloading",
    "download_progress_seen",
    "downloading_stalled",
    "downloading_allocating_loop",
    "start_failure",
    "entrypoint_started",
    "worker_heartbeat_seen",
    "gcs_bootstrap_seen",
    "worker_execution_started",
    "reallocation_required",
    "same_class_reallocation_started",
    "same_class_reallocation_succeeded",
    "same_class_reallocation_exhausted",
    "terminal_failed_with_evidence",
)

USER_VISIBLE_STATUS_MAP = {
    "submit_accepted": "queued",
    "container_group_created": "queued",
    "allocating": "provisioning",
    "instance_allocated": "provisioning",
    "downloading": "downloading",
    "download_progress_seen": "downloading",
    "downloading_stalled": "downloading",
    "downloading_allocating_loop": "recovering",
    "start_failure": "failed_with_evidence",
    "entrypoint_started": "running",
    "worker_heartbeat_seen": "running",
    "gcs_bootstrap_seen": "continuing",
    "worker_execution_started": "running",
    "reallocation_required": "recovering",
    "same_class_reallocation_started": "recovering",
    "same_class_reallocation_succeeded": "continuing",
    "same_class_reallocation_exhausted": "failed_with_evidence",
    "terminal_failed_with_evidence": "failed_with_evidence",
}


@dataclass(frozen=True)
class SaladBootstrapPolicy:
    """Thresholds for Salad bootstrap decisions.

    `warning_elapsed_seconds` is intentionally non-terminal. Official Salad docs say
    bootstrap can take 20 minutes or more, so this threshold only changes severity.
    """

    image_size_gb: float = 8.0
    warning_elapsed_seconds: int = 18 * 60
    allocation_stall_seconds: int = 45 * 60
    download_no_progress_seconds: int = 30 * 60
    download_base_seconds: int = 10 * 60
    download_seconds_per_gb: int = 3 * 60
    creating_stall_seconds: int = 40 * 60
    max_same_class_reallocation_attempts: int = 2
    allow_degraded_smoke_fallback: bool = False

    def expected_download_window_seconds(self, image_size_gb: Optional[float] = None) -> int:
        size = max(float(self.image_size_gb if image_size_gb is None else image_size_gb), 0.1)
        return max(
            self.download_no_progress_seconds,
            int(self.download_base_seconds + size * self.download_seconds_per_gb),
        )


@dataclass(frozen=True)
class SaladBootstrapObservation:
    job_id: str
    cg_name: str
    route_decision_id: str = ""
    output_gcs_prefix: str = ""
    requested_gpu_type: str = "RTX_5090"
    actual_gpu_type: str = ""
    allocation_attempt: int = 1
    poll: int = 0
    status_str: str = "pending"
    instance_state: str = ""
    instance_id: str = ""
    machine_id: str = ""
    latest_system_event: str = ""
    latest_system_event_time: str = ""
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    pulling_progress: Optional[float] = None
    previous_pulling_progress: Optional[float] = None
    elapsed_seconds: float = 0.0
    seconds_since_progress: Optional[float] = None
    image_size_gb: Optional[float] = None
    logs_seen: bool = False
    worker_heartbeat_seen: bool = False
    gcs_bootstrap_seen: bool = False
    worker_execution_started: bool = False
    completed_marker_present: bool = False
    failure_receipt_present: bool = False
    explicit_start_failure: bool = False
    exit_code: Optional[int] = None


@dataclass(frozen=True)
class SaladBootstrapDecision:
    state: str
    user_visible_status: str
    severity: str
    action: str
    reason_code: str
    reason: str
    terminal: bool = False
    reallocation_required: bool = False
    progress_seen: bool = False
    receipts_required: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "user_visible_status": self.user_visible_status,
            "severity": self.severity,
            "action": self.action,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "terminal": self.terminal,
            "reallocation_required": self.reallocation_required,
            "progress_seen": self.progress_seen,
            "receipts_required": list(self.receipts_required),
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_names(events: Iterable[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for item in events:
        name = str(item.get("event_name") or item.get("event") or "").strip()
        if name:
            names.append(name)
    return names


def _lowered_event_blob(observation: SaladBootstrapObservation) -> str:
    names = [observation.latest_system_event, *_event_names(observation.recent_events)]
    return " | ".join(name.lower() for name in names if name)


def _download_progress_advanced(observation: SaladBootstrapObservation) -> bool:
    if observation.pulling_progress is None:
        return False
    if observation.previous_pulling_progress is None:
        return float(observation.pulling_progress) > 0.0
    return float(observation.pulling_progress) > float(observation.previous_pulling_progress)


def _has_download_allocating_loop(observation: SaladBootstrapObservation) -> bool:
    names = [name.lower() for name in _event_names(observation.recent_events)]
    if observation.latest_system_event:
        names.insert(0, observation.latest_system_event.lower())
    downloading = sum(1 for name in names if "downloading" in name)
    allocating = sum(1 for name in names if "allocated" in name or "allocating" in name)
    reallocated = sum(1 for name in names if "reallocated" in name)
    return downloading >= 2 and (allocating >= 1 or reallocated >= 1)


def classify_salad_bootstrap(
    observation: SaladBootstrapObservation,
    policy: Optional[SaladBootstrapPolicy] = None,
) -> SaladBootstrapDecision:
    policy = policy or SaladBootstrapPolicy()
    status = str(observation.status_str or "").lower()
    instance_state = str(observation.instance_state or "").lower()
    event_blob = _lowered_event_blob(observation)
    elapsed = float(observation.elapsed_seconds or 0.0)
    no_progress = observation.seconds_since_progress
    progress_advanced = _download_progress_advanced(observation)
    expected_download = policy.expected_download_window_seconds(observation.image_size_gb)

    def decision(
        state: str,
        severity: str,
        action: str,
        reason_code: str,
        reason: str,
        *,
        terminal: bool = False,
        reallocation_required: bool = False,
        progress_seen: bool = False,
        receipts_required: Optional[List[str]] = None,
    ) -> SaladBootstrapDecision:
        return SaladBootstrapDecision(
            state=state,
            user_visible_status=USER_VISIBLE_STATUS_MAP.get(state, "provisioning"),
            severity=severity,
            action=action,
            reason_code=reason_code,
            reason=reason,
            terminal=terminal,
            reallocation_required=reallocation_required,
            progress_seen=progress_seen,
            receipts_required=list(receipts_required or ["provider_bootstrap_status_receipt"]),
        )

    if observation.failure_receipt_present or status == "failed":
        return decision(
            "terminal_failed_with_evidence",
            "failed",
            "terminal_failed",
            "provider_or_worker_failure_evidence",
            "Provider failed or worker failure receipt exists; failure is evidence-backed.",
            terminal=True,
            receipts_required=["provider_bootstrap_status_receipt", "start_failure_receipt"],
        )

    if observation.explicit_start_failure or "startfailure" in event_blob or "startup probe failure" in event_blob:
        return decision(
            "start_failure",
            "failed",
            "terminal_failed",
            "explicit_start_failure",
            "Salad system event reports an explicit start failure or startup probe failure.",
            terminal=True,
            receipts_required=["provider_bootstrap_status_receipt", "start_failure_receipt"],
        )

    if observation.exit_code not in (None, 0, 137):
        return decision(
            "terminal_failed_with_evidence",
            "failed",
            "terminal_failed",
            "nonzero_exit_evidence",
            "Salad system event reports a nonzero application exit.",
            terminal=True,
            receipts_required=["provider_bootstrap_status_receipt", "start_failure_receipt"],
        )

    if observation.completed_marker_present or observation.worker_execution_started:
        return decision(
            "worker_execution_started",
            "info",
            "observe",
            "gcs_worker_execution_progress",
            "Durable worker execution signal exists in GCS.",
            progress_seen=True,
            receipts_required=["provider_bootstrap_status_receipt", "provider_progress_receipt"],
        )

    if observation.gcs_bootstrap_seen:
        return decision(
            "gcs_bootstrap_seen",
            "info",
            "observe",
            "gcs_bootstrap_seen",
            "GCS bootstrap artifact or prefix exists; durable storage is the continuity authority.",
            progress_seen=True,
        )

    if observation.worker_heartbeat_seen:
        return decision(
            "worker_heartbeat_seen",
            "info",
            "observe",
            "worker_heartbeat_seen",
            "Worker heartbeat or latest_status.json indicates bootstrap progress.",
            progress_seen=True,
            receipts_required=["provider_bootstrap_status_receipt", "provider_progress_receipt"],
        )

    if status == "running" or instance_state == "running" or "instance starting" in event_blob:
        return decision(
            "entrypoint_started",
            "info",
            "observe",
            "entrypoint_or_container_started",
            "Container entrypoint/log/probe progress exists; do not kill while progress is moving.",
            progress_seen=True,
        )

    if _has_download_allocating_loop(observation):
        return decision(
            "downloading_allocating_loop",
            "warning",
            "reallocate",
            "downloading_allocating_loop",
            "Recent events show a repeated downloading/allocation loop distinct from simple capacity wait.",
            reallocation_required=True,
            receipts_required=[
                "provider_bootstrap_status_receipt",
                "provider_stall_receipt",
                "same_class_reallocation_receipt",
            ],
        )

    if instance_state == "downloading" or "instance downloading" in event_blob:
        if progress_advanced:
            return decision(
                "download_progress_seen",
                "info",
                "observe",
                "download_progress_advanced",
                "Image download progress advanced; do not kill prematurely.",
                progress_seen=True,
                receipts_required=["provider_bootstrap_status_receipt", "download_progress_receipt"],
            )
        if no_progress is not None and float(no_progress) >= expected_download:
            return decision(
                "reallocation_required",
                "warning",
                "reallocate",
                "download_exceeded_image_window_without_progress",
                "Downloading exceeded the image-size-adjusted expected window without progress.",
                reallocation_required=True,
                receipts_required=[
                    "provider_bootstrap_status_receipt",
                    "download_progress_receipt",
                    "provider_stall_receipt",
                    "same_class_reallocation_receipt",
                ],
            )
        if elapsed >= policy.warning_elapsed_seconds:
            return decision(
                "downloading_stalled",
                "warning",
                "observe",
                "download_warning_threshold_elapsed",
                "Downloading exceeded the warning window, but this is not terminal without stall evidence.",
                receipts_required=["provider_bootstrap_status_receipt", "download_progress_receipt"],
            )
        return decision(
            "downloading",
            "info",
            "observe",
            "download_in_progress",
            "Instance is downloading the image.",
            receipts_required=["provider_bootstrap_status_receipt", "download_progress_receipt"],
        )

    if instance_state == "creating" or "instance creating" in event_blob:
        if elapsed >= policy.creating_stall_seconds:
            return decision(
                "reallocation_required",
                "warning",
                "reallocate",
                "creating_stalled_beyond_policy",
                "Instance remained creating beyond policy; Salad docs recommend reallocation for stuck creating instances.",
                reallocation_required=True,
                receipts_required=["provider_bootstrap_status_receipt", "provider_stall_receipt", "same_class_reallocation_receipt"],
            )
        return decision(
            "entrypoint_started",
            "info",
            "observe",
            "instance_creating",
            "Node is preparing and starting the container image.",
            progress_seen=True,
        )

    if "instance allocated" in event_blob or instance_state == "allocated":
        return decision(
            "instance_allocated",
            "info",
            "observe",
            "instance_allocated",
            "Salad assigned a node; continue monitoring download/start progress.",
            progress_seen=True,
        )

    if elapsed >= policy.allocation_stall_seconds:
        return decision(
            "reallocation_required",
            "warning",
            "reallocate",
            "allocation_stalled_beyond_policy",
            "Container group remained allocation-stalled beyond policy; reallocate within the same GPU class.",
            reallocation_required=True,
            receipts_required=["provider_bootstrap_status_receipt", "provider_stall_receipt", "same_class_reallocation_receipt"],
        )

    if elapsed >= policy.warning_elapsed_seconds:
        return decision(
            "allocating",
            "warning",
            "observe",
            "allocation_warning_threshold_elapsed",
            "Allocation exceeded the warning window, but Salad docs allow 20+ minute starts; this is not terminal.",
        )

    if status in {"deploying", "pending"}:
        return decision(
            "allocating",
            "info",
            "observe",
            "allocation_or_deploying_in_progress",
            "Container group is accepted and still provisioning.",
        )

    return decision(
        "submit_accepted",
        "info",
        "observe",
        "submit_accepted",
        "Provider submission accepted; no terminal evidence exists.",
    )


def validate_same_class_gpu_policy(
    requested_gpu_type: str,
    effective_gpu_type: str,
    *,
    degraded_smoke_fallback: bool = False,
) -> Dict[str, Any]:
    requested = str(requested_gpu_type or "RTX_5090").strip().upper()
    effective = str(effective_gpu_type or requested).strip().upper()
    allowed = requested == effective
    reason_code = "same_class_gpu_request"
    reason = "Requested GPU class is preserved."
    if not allowed and requested == "RTX_5090" and effective == "RTX_4090":
        allowed = bool(degraded_smoke_fallback)
        reason_code = "explicit_degraded_smoke_fallback" if allowed else "cross_class_rtx4090_fallback_rejected"
        reason = (
            "Explicit degraded_smoke_fallback=true authorized RTX_4090 smoke fallback."
            if allowed
            else "RTX_4090 fallback is rejected for RTX_5090 unless degraded_smoke_fallback=true."
        )
    elif not allowed:
        reason_code = "cross_class_fallback_rejected"
        reason = "Cross-class GPU fallback is not allowed without an explicit degraded smoke policy."
    return {
        "schema_version": "salad_gpu_policy_decision_v1",
        "requested_gpu_type": requested,
        "effective_gpu_type": effective,
        "degraded_smoke_fallback": bool(degraded_smoke_fallback),
        "allowed": bool(allowed),
        "reason_code": reason_code,
        "reason": reason,
    }


def build_provider_bootstrap_status_receipt(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
    policy: SaladBootstrapPolicy,
) -> Dict[str, Any]:
    return {
        "schema_version": "provider_bootstrap_status_receipt_v1",
        "provider": "salad",
        "job_id": observation.job_id,
        "cg_name": observation.cg_name,
        "route_decision_id": observation.route_decision_id,
        "output_gcs_prefix": observation.output_gcs_prefix,
        "requested_gpu_type": observation.requested_gpu_type,
        "actual_gpu_type": observation.actual_gpu_type,
        "allocation_attempt": int(observation.allocation_attempt or 1),
        "poll": int(observation.poll or 0),
        "status_str": observation.status_str,
        "instance_state": observation.instance_state,
        "instance_id": observation.instance_id,
        "machine_id": observation.machine_id,
        "latest_system_event": observation.latest_system_event,
        "latest_system_event_time": observation.latest_system_event_time,
        "pulling_progress": observation.pulling_progress,
        "previous_pulling_progress": observation.previous_pulling_progress,
        "elapsed_seconds": round(float(observation.elapsed_seconds or 0.0), 1),
        "seconds_since_progress": observation.seconds_since_progress,
        "expected_download_window_seconds": policy.expected_download_window_seconds(observation.image_size_gb),
        "warning_elapsed_seconds": policy.warning_elapsed_seconds,
        "decision": decision.as_dict(),
        "gcs_bootstrap_seen": bool(observation.gcs_bootstrap_seen),
        "worker_heartbeat_seen": bool(observation.worker_heartbeat_seen),
        "worker_execution_started": bool(observation.worker_execution_started),
        "completed_marker_present": bool(observation.completed_marker_present),
        "failure_receipt_present": bool(observation.failure_receipt_present),
        "official_docs_authority": [
            ".mica/external_docs/salad/container-engine/explanation/container-groups/deployment-lifecycle.mdx",
            ".mica/external_docs/salad/container-engine/explanation/container-groups/system-events.mdx",
            ".mica/external_docs/salad/container-engine/explanation/container-groups/container-groups.mdx",
        ],
        "produced_at": utc_now_iso(),
    }


def build_download_progress_receipt(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
    policy: SaladBootstrapPolicy,
) -> Dict[str, Any]:
    return {
        "schema_version": "download_progress_receipt_v1",
        "provider": "salad",
        "job_id": observation.job_id,
        "cg_name": observation.cg_name,
        "instance_id": observation.instance_id,
        "status_str": observation.status_str,
        "pulling_progress": observation.pulling_progress,
        "previous_pulling_progress": observation.previous_pulling_progress,
        "progress_seen": bool(decision.progress_seen),
        "elapsed_seconds": round(float(observation.elapsed_seconds or 0.0), 1),
        "seconds_since_progress": observation.seconds_since_progress,
        "expected_download_window_seconds": policy.expected_download_window_seconds(observation.image_size_gb),
        "decision_state": decision.state,
        "produced_at": utc_now_iso(),
    }


def build_provider_stall_receipt(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
    policy: SaladBootstrapPolicy,
) -> Dict[str, Any]:
    return {
        "schema_version": "provider_stall_receipt_v1",
        "provider": "salad",
        "job_id": observation.job_id,
        "cg_name": observation.cg_name,
        "route_decision_id": observation.route_decision_id,
        "instance_id": observation.instance_id,
        "machine_id": observation.machine_id,
        "requested_gpu_type": observation.requested_gpu_type,
        "allocation_attempt": int(observation.allocation_attempt or 1),
        "stall_state": decision.state,
        "reason_code": decision.reason_code,
        "elapsed_seconds": round(float(observation.elapsed_seconds or 0.0), 1),
        "seconds_since_progress": observation.seconds_since_progress,
        "latest_system_event": observation.latest_system_event,
        "recent_events": list(observation.recent_events),
        "reallocation_required": bool(decision.reallocation_required),
        "terminal": bool(decision.terminal),
        "produced_at": utc_now_iso(),
    }


def build_same_class_reallocation_receipt(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
    *,
    target_gpu_type: Optional[str] = None,
    reallocation_started: bool = False,
    reallocation_succeeded: bool = False,
    reallocation_exhausted: bool = False,
) -> Dict[str, Any]:
    target = target_gpu_type or observation.requested_gpu_type
    gpu_policy = validate_same_class_gpu_policy(
        observation.requested_gpu_type,
        target,
        degraded_smoke_fallback=False,
    )
    return {
        "schema_version": "same_class_reallocation_receipt_v1",
        "provider": "salad",
        "job_id": observation.job_id,
        "cg_name": observation.cg_name,
        "route_decision_id": observation.route_decision_id,
        "output_gcs_prefix": observation.output_gcs_prefix,
        "instance_id": observation.instance_id,
        "requested_gpu_type": observation.requested_gpu_type,
        "target_gpu_type": target,
        "gpu_policy": gpu_policy,
        "allocation_attempt": int(observation.allocation_attempt or 1),
        "next_allocation_attempt": int(observation.allocation_attempt or 1) + 1,
        "reallocation_reason_code": decision.reason_code,
        "reallocation_started": bool(reallocation_started),
        "reallocation_succeeded": bool(reallocation_succeeded),
        "reallocation_exhausted": bool(reallocation_exhausted),
        "job_id_preserved": True,
        "route_decision_id_preserved": bool(observation.route_decision_id),
        "gcs_prefix_preserved": bool(observation.output_gcs_prefix),
        "produced_at": utc_now_iso(),
    }


def build_start_failure_receipt(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
) -> Dict[str, Any]:
    return {
        "schema_version": "start_failure_receipt_v1",
        "provider": "salad",
        "job_id": observation.job_id,
        "cg_name": observation.cg_name,
        "route_decision_id": observation.route_decision_id,
        "instance_id": observation.instance_id,
        "machine_id": observation.machine_id,
        "failure_mode": decision.reason_code,
        "failure_detail": decision.reason,
        "status_str": observation.status_str,
        "latest_system_event": observation.latest_system_event,
        "exit_code": observation.exit_code,
        "elapsed_seconds": round(float(observation.elapsed_seconds or 0.0), 1),
        "terminal_failed_with_evidence": bool(decision.terminal),
        "produced_at": utc_now_iso(),
    }


def build_user_visible_continuity_receipt(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
) -> Dict[str, Any]:
    if decision.user_visible_status == "failed_with_evidence":
        message = "Salad bootstrap failed with provider evidence; durable GCS custody was checked before closure."
        can_retry = decision.reason_code not in {"cross_class_rtx4090_fallback_rejected"}
    elif decision.user_visible_status in {"provisioning", "downloading"}:
        message = "Salad is still provisioning; startup can take 20 minutes or more and will not be killed while progress is moving."
        can_retry = False
    elif decision.user_visible_status == "recovering":
        message = "Salad bootstrap appears stalled; same-class reallocation is the next recovery action."
        can_retry = True
    else:
        message = "Salad bootstrap is progressing; durable GCS artifacts remain the continuity authority."
        can_retry = False
    return {
        "schema_version": "user_visible_continuity_receipt_v1",
        "provider": "salad",
        "job_id": observation.job_id,
        "cg_name": observation.cg_name,
        "route_decision_id": observation.route_decision_id,
        "user_visible_status": decision.user_visible_status,
        "user_message": message,
        "can_retry": bool(can_retry),
        "retry_strategy": "same_class_reallocation" if decision.reallocation_required else "observe_progress",
        "packet_custody_state": "complete" if observation.completed_marker_present else "partial" if observation.gcs_bootstrap_seen else "staged_only",
        "gcs_prefix": observation.output_gcs_prefix,
        "stop_is_pause": False,
        "durable_authority": "gcs",
        "produced_at": utc_now_iso(),
    }


def build_policy_receipts(
    observation: SaladBootstrapObservation,
    decision: SaladBootstrapDecision,
    policy: Optional[SaladBootstrapPolicy] = None,
) -> Dict[str, Any]:
    policy = policy or SaladBootstrapPolicy()
    receipts: Dict[str, Any] = {
        "provider_bootstrap_status_receipt": build_provider_bootstrap_status_receipt(observation, decision, policy),
        "user_visible_continuity_receipt": build_user_visible_continuity_receipt(observation, decision),
    }
    if "download_progress_receipt" in decision.receipts_required:
        receipts["download_progress_receipt"] = build_download_progress_receipt(observation, decision, policy)
    if "provider_stall_receipt" in decision.receipts_required:
        receipts["provider_stall_receipt"] = build_provider_stall_receipt(observation, decision, policy)
    if "same_class_reallocation_receipt" in decision.receipts_required:
        receipts["same_class_reallocation_receipt"] = build_same_class_reallocation_receipt(observation, decision)
    if "start_failure_receipt" in decision.receipts_required:
        receipts["start_failure_receipt"] = build_start_failure_receipt(observation, decision)
    return receipts