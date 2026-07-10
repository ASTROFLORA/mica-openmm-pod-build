from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from mica.api_v1.health_probes import probe_all
from mica.config.dotenv_loader import seed_env_from_dotenv


@dataclass(frozen=True)
class PreflightDecision:
    status: str
    not_durable: bool
    reason_code: str
    remediation_hint: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "not_durable": self.not_durable,
            "reason_code": self.reason_code,
            "remediation_hint": self.remediation_hint,
        }


def decide_preflight(
    probes: Dict[str, Any],
    *,
    require_neon: bool,
    require_timescale: bool,
    strict_durable: bool,
) -> PreflightDecision:
    neon = dict(probes.get("neon") or {})
    timescale = dict(probes.get("timescale") or {})

    neon_configured = bool(neon.get("configured"))
    timescale_configured = bool(timescale.get("configured"))

    neon_ok = str(neon.get("status") or "") == "ok"
    timescale_ok = str(timescale.get("status") or "") == "ok"

    if require_neon and not neon_configured:
        return PreflightDecision(
            status="blocked",
            not_durable=True,
            reason_code="neon_not_configured",
            remediation_hint="Set NEON_DATABASE_URL and verify network/auth before production E2E.",
        )

    if require_timescale and not timescale_configured:
        return PreflightDecision(
            status="blocked",
            not_durable=True,
            reason_code="timescale_not_configured",
            remediation_hint="Set TIMESCALE_SERVICE_URL/TIMESCALE_DSN and verify DSN authority.",
        )

    if require_neon and not neon_ok:
        return PreflightDecision(
            status="blocked",
            not_durable=True,
            reason_code="neon_probe_error",
            remediation_hint="Fix Neon reachability and credentials before proceeding.",
        )

    # Timescale is the durable temporal ledger authority for this gate.
    if require_timescale and not timescale_ok:
        status = "blocked" if strict_durable else "degraded"
        return PreflightDecision(
            status=status,
            not_durable=True,
            reason_code="timescale_probe_error",
            remediation_hint=(
                "Resolve Timescale SSL/connectivity failures (check sslmode, cert chain, "
                "MICA_SSL_VERIFY_SKIP policy) and rerun preflight."
            ),
        )

    return PreflightDecision(
        status="pass",
        not_durable=False,
        reason_code="ok",
        remediation_hint="none",
    )


async def run_db_preflight(
    *,
    timeout_seconds: float = 1.5,
    require_neon: bool = True,
    require_timescale: bool = True,
    strict_durable: bool = True,
) -> Dict[str, Any]:
    # Keep preflight consistent with app/worker startup behavior.
    seed_env_from_dotenv()
    probes = await probe_all(timeout=timeout_seconds)
    decision = decide_preflight(
        probes,
        require_neon=require_neon,
        require_timescale=require_timescale,
        strict_durable=strict_durable,
    )
    return {
        "timeout_seconds": timeout_seconds,
        "requirements": {
            "require_neon": require_neon,
            "require_timescale": require_timescale,
            "strict_durable": strict_durable,
        },
        "decision": decision.to_dict(),
        "probes": probes,
    }
