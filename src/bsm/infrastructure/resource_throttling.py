"""Resource throttling heuristics for BioDynamo and SuperDynamo workloads."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


@dataclass
class ThrottleDecision:
    """Represents the outcome of a throttling heuristic evaluation."""

    recommended_cpu_limit: int
    recommended_gpu_limit: int
    recommended_concurrency: int
    cooldown_seconds: int
    reasons: Iterable[str]
    applied: bool

    def to_metrics(self) -> Dict[str, Any]:
        """Return a JSON-serializable metrics payload for telemetry."""

        return {
            "cpu_limit": self.recommended_cpu_limit,
            "gpu_limit": self.recommended_gpu_limit,
            "concurrency_limit": self.recommended_concurrency,
            "cooldown_seconds": self.cooldown_seconds,
            "applied": self.applied,
            "reasons": list(self.reasons),
        }


class ResourceThrottleAdvisor:
    """Applies lightweight heuristics to balance BioDynamo resource usage."""

    def __init__(
        self,
        *,
        max_gpu_per_job: Optional[int] = None,
        max_cpu_per_job: Optional[int] = None,
        max_concurrency: Optional[int] = None,
        high_atom_threshold: Optional[int] = None,
    ) -> None:
        self.max_gpu_per_job = max_gpu_per_job or self._env_int("BSM_MAX_GPU_PER_JOB", default=2)
        self.max_cpu_per_job = max_cpu_per_job or self._env_int("BSM_MAX_CPU_PER_JOB", default=16)
        self.max_concurrency = max_concurrency or self._env_int("BSM_MAX_CONCURRENCY", default=2)
        self.high_atom_threshold = high_atom_threshold or self._env_int("BSM_HIGH_ATOM_THRESHOLD", default=75000)

    def evaluate(
        self,
        params: Dict[str, Any],
        telemetry_metrics: Optional[Dict[str, Any]] = None,
    ) -> ThrottleDecision:
        """Return throttle decision based on simulation parameters and telemetry metrics."""

        reasons: list[str] = []

        requested_gpus = max(1, int(params.get("num_gpus", 1)))
        requested_cpus = max(1, int(params.get("cpu_limit", self.max_cpu_per_job)))
        requested_concurrency = max(1, int(params.get("concurrent_jobs", 1)))

        recommended_gpu = min(requested_gpus, self.max_gpu_per_job)
        recommended_cpu = min(requested_cpus, self.max_cpu_per_job)
        recommended_concurrency = min(requested_concurrency, self.max_concurrency)
        cooldown_seconds = 0

        atom_count = int(params.get("n_atoms", 0))
        priority = params.get("priority", "normal")

        if atom_count >= self.high_atom_threshold:
            reasons.append("high_atom_count")
            recommended_concurrency = 1
            recommended_gpu = max(1, min(recommended_gpu, self.max_gpu_per_job // 2 or 1))
            cooldown_seconds = max(cooldown_seconds, 45)

        if priority == "low":
            reasons.append("low_priority")
            recommended_gpu = max(1, min(recommended_gpu, 1))
            recommended_cpu = max(1, min(recommended_cpu, self.max_cpu_per_job // 2 or 1))
            cooldown_seconds = max(cooldown_seconds, 30)

        if telemetry_metrics:
            queue_depth = int(telemetry_metrics.get("queue_depth", 0))
            gpu_utilization = float(telemetry_metrics.get("gpu_utilization", 0.0))
            if queue_depth >= 5:
                reasons.append("queue_backlog")
                recommended_concurrency = max(1, min(recommended_concurrency, 1))
                cooldown_seconds = max(cooldown_seconds, 60)
            if gpu_utilization >= 0.85:
                reasons.append("gpu_pressure")
                recommended_gpu = max(1, min(recommended_gpu, self.max_gpu_per_job - 1 or 1))

        applied = (
            recommended_gpu != requested_gpus
            or recommended_cpu != requested_cpus
            or recommended_concurrency != requested_concurrency
        )

        if not reasons:
            reasons.append("baseline_policy")

        return ThrottleDecision(
            recommended_cpu_limit=recommended_cpu,
            recommended_gpu_limit=recommended_gpu,
            recommended_concurrency=recommended_concurrency,
            cooldown_seconds=cooldown_seconds,
            reasons=reasons,
            applied=applied,
        )

    def apply(self, params: Dict[str, Any], decision: ThrottleDecision) -> Dict[str, Any]:
        """Return a copy of params with throttling decisions applied."""

        updated = dict(params)
        updated["num_gpus"] = decision.recommended_gpu_limit
        updated["cpu_limit"] = decision.recommended_cpu_limit
        updated["concurrent_jobs"] = decision.recommended_concurrency
        updated["resource_throttle"] = decision.to_metrics()
        if decision.cooldown_seconds:
            updated["throttle_cooldown_seconds"] = decision.cooldown_seconds
        return updated

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, ""))
        except ValueError:
            value = default
        return value or default
