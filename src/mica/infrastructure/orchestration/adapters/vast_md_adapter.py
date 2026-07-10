from __future__ import annotations

from typing import Any, Dict

from mica.drivers.md_execution_contract import (
    enforce_no_silent_success,
    normalize_remote_execution_result,
)

from ..biostate_engine_job import merge_biostate_execution_request
from ..md_provider_adapter import MDProviderAdapter


def _phase_value(raw_result: Any, state_json: Dict[str, Any]) -> str:
    phase = state_json.get("phase") or getattr(getattr(raw_result, "phase", None), "value", "")
    return str(phase or "unknown").lower()


class VastMDAdapter(MDProviderAdapter):
    provider_aliases = ("vast",)
    adapter_id = "vast_md_adapter"

    def build_orchestrator(self, cfg: Any, provider: Any, on_event: Any = None) -> Any:
        from ..vast_md_orchestrator import VastMDOrchestrator

        return VastMDOrchestrator(config=cfg, provider=provider, on_event=on_event)

    def build_request(self, cfg: Any, provider_name: str) -> Dict[str, Any]:
        return merge_biostate_execution_request(
            cfg,
            provider_name,
            default_template_id="cl06_vast_compat",
            default_checkpoint_policy="strict",
            default_storage_backend="none",
        )

    def normalize_result(
        self,
        raw_result: Any,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        state_json = raw_result.to_dict() if hasattr(raw_result, "to_dict") else dict(raw_result or {})
        phase = _phase_value(raw_result, state_json)
        raw_status = "completed" if phase == "complete" else ("failed_recoverable" if phase == "failed_recoverable" else "failed")
        raw_payload = {
            "status": raw_status,
            "success": phase == "complete",
            "results_json": state_json,
            "output_dir": str(state_json.get("local_output_dir", "") or ""),
            "vast_phase_final": phase,
            "error": str(state_json.get("error", "") or ""),
            "adapter_id": self.adapter_id,
            "execution_mode": f"remote_{provider_name or 'vast'}",
        }
        canonical = normalize_remote_execution_result(raw_payload, request)
        preserved_for_recovery = bool(
            state_json.get("teardown_unconfirmed")
            or (phase == "failed" and getattr(orchestrator.cfg, "preserve_instance_on_failure", True))
        )
        canonical["provider"] = {
            "name": provider_name,
            "adapter_id": self.adapter_id,
            "instance_id": str(state_json.get("instance_id", "") or ""),
            "ssh_host": str(state_json.get("ssh_host", "") or ""),
            "run_dir": str(state_json.get("run_dir", "") or ""),
            "total_cost_usd": float(state_json.get("total_cost_usd", 0.0) or 0.0),
        }
        canonical["terminal_autopsy"] = {
            "schema_version": "terminal_autopsy_v1",
            "terminal_state": phase,
            "reason_code": "provider_terminal_state" if phase != "complete" else "",
            "reason_message": str(state_json.get("error", "") or ""),
            "metadata": {
                "instance_id": str(state_json.get("instance_id", "") or ""),
                "safe_stop_completed": bool(state_json.get("safe_stop_completed", False)),
                "stop_reason": str(state_json.get("stop_reason", "") or ""),
                "scientific_completion_achieved": bool(state_json.get("scientific_completion_achieved", False)),
                "destroy_attempted": bool(state_json.get("destroy_attempted", False)),
                "destroy_succeeded": bool(state_json.get("destroy_succeeded", False)),
                "teardown_unconfirmed": bool(state_json.get("teardown_unconfirmed", False)),
                "teardown_failure_reason": str(state_json.get("teardown_failure_reason", "") or ""),
            },
        }
        canonical["teardown_proof"] = {
            "schema_version": "teardown_proof_v1",
            "destroy_attempted": bool(state_json.get("destroy_attempted", not preserved_for_recovery)),
            "destroy_succeeded": bool(state_json.get("destroy_succeeded", phase == "complete" and not preserved_for_recovery)),
            "preserved_for_recovery": preserved_for_recovery,
            "destroy_skipped_reason": str(state_json.get("teardown_failure_reason", "") or ""),
            "metadata": {
                "proof_class": "synthetic_from_legacy_orchestrator",
                "scientific_completion_achieved": bool(state_json.get("scientific_completion_achieved", False)),
            },
        }
        return enforce_no_silent_success(canonical)
