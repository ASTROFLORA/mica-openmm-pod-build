from __future__ import annotations

from typing import Any, Dict

from mica.infrastructure.orchestration.sp02_parity import LANE_ID, build_sp02_packet

from .vast_md_adapter import VastMDAdapter


class RunPodMDAdapter(VastMDAdapter):
    provider_aliases = ("runpod", "runpod_pods")
    adapter_id = "runpod_md_adapter"

    def normalize_result(
        self,
        raw_result: Any,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        canonical = super().normalize_result(
            raw_result=raw_result,
            request=request,
            orchestrator=orchestrator,
            provider_name=provider_name,
        )

        job_id = str(request.get("job", {}).get("job_id", "") or "")
        # For RunPod (via VastMDOrchestrator), instance_id carries the provider-side handle.
        provider_job_id = str(canonical.get("provider", {}).get("instance_id", "") or "")

        teardown = canonical.get("teardown_proof") or {}
        artifacts_raw = canonical.get("artifacts") or []
        if not isinstance(artifacts_raw, list):
            artifacts_raw = []

        sp02_payload: Dict[str, Any] = {
            "session_id": job_id,
            "job_id": job_id,
            "provider_job_id": provider_job_id,
            "lane_id": LANE_ID,
            "provider": provider_name,
            "bridge": "sim:job/submit",
            "accepted": bool(canonical.get("job", {}).get("success", False)),
            "submit_accepted": bool(provider_job_id),
            "success": bool(canonical.get("job", {}).get("success", False)),
            "error": str(canonical.get("job", {}).get("error", "") or ""),
            "terminal_state": str(
                canonical.get("provider", {}).get("phase", "")
                or getattr(getattr(raw_result, "phase", None), "value", "")
                or ""
            ),
            "output_dir": str(canonical.get("job", {}).get("output_dir", "") or ""),
            "artifacts": artifacts_raw,
            "teardown_proof": teardown,
            "orphan_scan_result": str(
                (teardown.get("orphan_scan_result") if isinstance(teardown, dict) else "")
                or "not_scanned"
            ),
        }

        canonical["sp02_packet"] = build_sp02_packet(sp02_payload)
        canonical["lane_id"] = LANE_ID
        return canonical