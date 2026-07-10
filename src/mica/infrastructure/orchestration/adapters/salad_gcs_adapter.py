from __future__ import annotations

from typing import Any, Dict

from mica.drivers.md_execution_contract import (
    enforce_no_silent_success,
    normalize_salad_execution_result,
)

from ..biostate_engine_job import merge_biostate_execution_request
from ..md_provider_adapter import MDProviderAdapter
from ..salad_gcs_orchestrator import SaladGCSOrchestrator, SaladMDJobConfig


def _artifact_manifest_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    artifact_manifest = dict(payload.get("artifact_manifest") or {})
    if artifact_manifest:
        return artifact_manifest

    durability_evidence = dict(payload.get("durability_evidence") or {})
    runtime_signal = dict((payload.get("terminal_autopsy") or {}).get("runtime_signal") or {})
    object_listing = list(
        durability_evidence.get("object_listing")
        or runtime_signal.get("object_listing")
        or []
    )

    return {
        "completed_marker_confirmed": bool(
            durability_evidence.get("completed_marker_present")
            or runtime_signal.get("completed_marker_present")
        ),
        "dcd_chunk_count": int(
            durability_evidence.get("dcd_chunk_count")
            or runtime_signal.get("dcd_chunk_count")
            or 0
        ),
        "history_json_present": bool(
            durability_evidence.get("history_json_present")
            or runtime_signal.get("history_json_present")
        ),
        "worker_history_json_present": bool(
            durability_evidence.get("worker_history_json_present")
            or runtime_signal.get("worker_history_json_present")
        ),
        "failure_receipt_present": bool(
            durability_evidence.get("failure_receipt_present")
            or runtime_signal.get("failure_receipt_present")
        ),
        "failure_traceback_present": bool(
            durability_evidence.get("failure_traceback_present")
            or runtime_signal.get("failure_traceback_present")
        ),
        "object_listing": object_listing,
    }


class SaladGCSAdapter(MDProviderAdapter):
    provider_aliases = ("salad",)
    adapter_id = "salad_gcs_adapter"

    def supports_config(self, cfg: Any) -> bool:
        return isinstance(cfg, SaladMDJobConfig)

    def build_orchestrator(self, cfg: Any, provider: Any, on_event: Any = None) -> Any:
        return SaladGCSOrchestrator(config=cfg, provider=provider, on_event=on_event)

    def build_request(self, cfg: Any, provider_name: str) -> Dict[str, Any]:
        return merge_biostate_execution_request(
            cfg,
            provider_name,
            default_template_id="cl06_salad_compat",
            default_checkpoint_policy="none",
            default_storage_backend="gcs",
        )

    def normalize_result(
        self,
        raw_result: Any,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        payload = dict(raw_result or {})
        payload["artifact_manifest"] = _artifact_manifest_from_payload(payload)
        canonical = normalize_salad_execution_result(payload, request)
        status_str = str(payload.get("status", "unknown") or "unknown").lower()
        if status_str == "timeout":
            canonical["status"]["state"] = "timeout"
            canonical["status"]["terminal"] = True
            canonical["status"]["success"] = False
            if not canonical["status"].get("reason_code"):
                canonical["status"]["reason_code"] = "timeout"
        elif status_str in {"failed", "error", "stopped"}:
            canonical["status"]["state"] = "failed"
            canonical["status"]["terminal"] = True
            canonical["status"]["success"] = False
            if not canonical["status"].get("reason_code"):
                canonical["status"]["reason_code"] = status_str

        canonical["provider"] = {
            **dict(canonical.get("provider") or {}),
            "name": provider_name,
            "adapter_id": self.adapter_id,
        }
        canonical["terminal_autopsy"] = dict(payload.get("terminal_autopsy") or {})
        canonical["teardown_proof"] = dict(payload.get("teardown_proof") or {})
        canonical["artifact_state"] = str(payload.get("artifact_state", "") or "")
        canonical["durability_evidence"] = dict(payload.get("durability_evidence") or {})
        return enforce_no_silent_success(canonical)
