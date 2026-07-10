"""SP-05 PROVIDER_TEARDOWN_AND_ORPHAN_CONTROL — standardized failure taxonomy
and mandatory teardown/orphan sweep evidence for all compute providers.

Doctrine:
  Every compute provider (Vast.ai, RunPod, Modal) MUST emit a conformant
  ProviderTeardownRecord when a job terminates in any failure mode OR when an
  orphan sweep detects an abandoned instance.

  "Orphan" = a provider instance whose MICA execution record is absent or in a
  non-terminal state while the provider reports the instance as RUNNING or ACTIVE
  beyond a staleness threshold.

  This module is deterministic (no network required). It encodes:
    - the canonical MICA provider registry
    - the ProviderFailureTaxonomy (8 canonical failure types)
    - ProviderTeardownRecord contract and validator
    - OrphanSweepRecord contract and validator
    - synthetic record builders for proof
    - run_provider_teardown_proof() — 26-step structured proof

Canonical Provider Registry:
  vast    — Vast.ai GPU marketplace (VastMDOrchestrator, VastCLI)
  runpod  — RunPod serverless/pod workers
  modal   — Modal Labs serverless compute plane

Provider Failure Taxonomy (canonical 8 types):
  user_cancelled       → explicit user cancellation request
  provider_evicted     → provider reclaimed instance (spot/preemption)
  health_check_timeout → heartbeat / health-probe exceeded wall-clock limit
  oom_killed           → OOM kill from provider or container runtime
  network_partition    → connectivity loss between MICA and provider
  provider_api_error   → provider API returned a 5xx / unrecoverable error
  orphan_detected      → sweep found an instance without a live MICA record
  unknown_failure      → no classifiable root cause (catch-all; must log raw reason)

Teardown states (per provider instance):
  completed        → instance cleanly destroyed and confirmed absent
  skipped          → teardown not attempted (e.g., serverless provider-managed)
  failed           → destroy was attempted but provider rejected / timed out
  not_applicable   → Modal serverless lifecycle is provider-managed
  pending_sweep    → detected as orphan; sweep queued but not yet executed

Required fields in every ProviderTeardownRecord:
  execution_id, provider, provider_job_id, provider_instance_id
  lane, failure_type, teardown_state
  destroy_attempted, destroy_succeeded
  storage_authority  (== COMPUTE_STORAGE_AUTHORITY)
  started_at, finished_at
  orphan_sweep_triggered   (bool)
  residual_instance_ids    (list[str] — empty when teardown succeeded)
  raw_failure_reason       (str — required when failure_type != "user_cancelled")

Required fields in every OrphanSweepRecord:
  sweep_id, provider, sweep_triggered_at, sweep_completed_at
  instances_scanned, orphans_detected, orphans_destroyed, orphans_preserved
  sweep_status (completed | partial | failed)
  evidence_uri  (GCS path to sweep artifact; may be empty in proof runs)
  failure_details (list[str] — empty on clean sweep)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mica.config.dotenv_loader import seed_env_from_dotenv
from mica.storage.compute_durability import COMPUTE_STORAGE_AUTHORITY

# ── Provider registry ─────────────────────────────────────────────────────────

PROVIDER_REGISTRY: frozenset[str] = frozenset({"vast", "runpod", "modal"})

# Provider → lane authority mapping
PROVIDER_LANE_MAP: dict[str, str] = {
    "vast": "remote_md",
    "runpod": "serverless",
    "modal": "serverless",
}

# Serverless providers where teardown is provider-managed
SERVERLESS_PROVIDERS: frozenset[str] = frozenset({"runpod", "modal"})

# ── Failure taxonomy ──────────────────────────────────────────────────────────

PROVIDER_FAILURE_TAXONOMY: frozenset[str] = frozenset({
    "user_cancelled",
    "provider_evicted",
    "health_check_timeout",
    "oom_killed",
    "network_partition",
    "provider_api_error",
    "orphan_detected",
    "unknown_failure",
})

# Failure types that require a non-empty raw_failure_reason
FAILURE_TYPES_REQUIRING_REASON: frozenset[str] = PROVIDER_FAILURE_TAXONOMY - frozenset({"user_cancelled"})

# ── Teardown states ────────────────────────────────────────────────────────────

TEARDOWN_STATES: frozenset[str] = frozenset({
    "completed",
    "skipped",
    "failed",
    "not_applicable",
    "pending_sweep",
})

# ── Sweep states ──────────────────────────────────────────────────────────────

SWEEP_STATES: frozenset[str] = frozenset({"completed", "partial", "failed"})

# ── Staleness threshold (seconds) for orphan detection ────────────────────────
ORPHAN_STALENESS_THRESHOLD_S: int = 1800  # 30 min


# ── Validation results ─────────────────────────────────────────────────────────

@dataclass
class TeardownValidationResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


# ── Teardown record validator ─────────────────────────────────────────────────

def validate_provider_teardown_record(record: dict[str, Any]) -> TeardownValidationResult:
    """Validate a ProviderTeardownRecord against the SP-05 contract.

    Args:
        record: The teardown record dict.

    Returns:
        TeardownValidationResult with .ok, .errors, .warnings.
    """
    r = TeardownValidationResult()
    rec = record or {}

    # ── Identity ──────────────────────────────────────────────────────────────
    if not str(rec.get("execution_id") or "").strip():
        r.fail("execution_id: missing or empty")

    # ── Provider ──────────────────────────────────────────────────────────────
    provider = str(rec.get("provider") or "").strip()
    if provider not in PROVIDER_REGISTRY:
        r.fail(f"provider: '{provider}' not in registry {sorted(PROVIDER_REGISTRY)}")

    if not str(rec.get("provider_job_id") or "").strip():
        r.warn("provider_job_id: empty — recommended for provider traceability")

    if not str(rec.get("provider_instance_id") or "").strip():
        r.warn("provider_instance_id: empty — recommended for instance traceability")

    # ── Lane ──────────────────────────────────────────────────────────────────
    lane = str(rec.get("lane") or "").strip()
    if lane not in {"remote_md", "serverless", "job"}:
        r.fail(f"lane: '{lane}' not valid — must be one of {{remote_md, serverless, job}}")

    # ── Failure taxonomy ──────────────────────────────────────────────────────
    failure_type = str(rec.get("failure_type") or "").strip()
    if failure_type not in PROVIDER_FAILURE_TAXONOMY:
        r.fail(
            f"failure_type: '{failure_type}' not in taxonomy {sorted(PROVIDER_FAILURE_TAXONOMY)}"
        )

    # ── Failure reason ────────────────────────────────────────────────────────
    if failure_type in FAILURE_TYPES_REQUIRING_REASON:
        if not str(rec.get("raw_failure_reason") or "").strip():
            r.fail(
                f"raw_failure_reason: required for failure_type='{failure_type}' but missing or empty"
            )

    # ── Teardown state ────────────────────────────────────────────────────────
    teardown_state = str(rec.get("teardown_state") or "").strip()
    if teardown_state not in TEARDOWN_STATES:
        r.fail(f"teardown_state: '{teardown_state}' not in {sorted(TEARDOWN_STATES)}")

    # ── Destroy fields ────────────────────────────────────────────────────────
    if "destroy_attempted" not in rec:
        r.fail("destroy_attempted: missing")
    if "destroy_succeeded" not in rec:
        r.fail("destroy_succeeded: missing")

    # Serverless providers use provider-managed lifecycle
    is_serverless = provider in SERVERLESS_PROVIDERS
    if is_serverless and teardown_state not in {"not_applicable", "skipped"}:
        r.warn(
            f"provider '{provider}' is serverless — teardown_state expected "
            f"'not_applicable' or 'skipped', got '{teardown_state}'"
        )

    # ── Storage authority ─────────────────────────────────────────────────────
    storage_auth = str(rec.get("storage_authority") or "").strip()
    if storage_auth != COMPUTE_STORAGE_AUTHORITY:
        r.fail(
            f"storage_authority: expected '{COMPUTE_STORAGE_AUTHORITY}', got '{storage_auth}'"
        )

    # ── Timestamps ────────────────────────────────────────────────────────────
    if not str(rec.get("started_at") or "").strip():
        r.fail("started_at: missing or empty")
    if not str(rec.get("finished_at") or "").strip():
        r.fail("finished_at: missing or empty")

    # ── Orphan sweep ─────────────────────────────────────────────────────────
    if "orphan_sweep_triggered" not in rec:
        r.fail("orphan_sweep_triggered: missing — must be a bool")

    # ── Residual instances ────────────────────────────────────────────────────
    if not isinstance(rec.get("residual_instance_ids"), list):
        r.fail("residual_instance_ids: missing or not a list")

    return r


# ── Orphan sweep record validator ─────────────────────────────────────────────

def validate_orphan_sweep_record(record: dict[str, Any]) -> TeardownValidationResult:
    """Validate an OrphanSweepRecord against the SP-05 contract."""
    r = TeardownValidationResult()
    rec = record or {}

    if not str(rec.get("sweep_id") or "").strip():
        r.fail("sweep_id: missing or empty")

    provider = str(rec.get("provider") or "").strip()
    if provider not in PROVIDER_REGISTRY:
        r.fail(f"provider: '{provider}' not in registry {sorted(PROVIDER_REGISTRY)}")

    if not str(rec.get("sweep_triggered_at") or "").strip():
        r.fail("sweep_triggered_at: missing or empty")
    if not str(rec.get("sweep_completed_at") or "").strip():
        r.fail("sweep_completed_at: missing or empty")

    sweep_status = str(rec.get("sweep_status") or "").strip()
    if sweep_status not in SWEEP_STATES:
        r.fail(f"sweep_status: '{sweep_status}' not in {sorted(SWEEP_STATES)}")

    for field_name in ("instances_scanned", "orphans_detected", "orphans_destroyed", "orphans_preserved"):
        val = rec.get(field_name)
        if not isinstance(val, int):
            r.fail(f"{field_name}: must be an int, got {type(val).__name__}")

    if not isinstance(rec.get("failure_details"), list):
        r.fail("failure_details: missing or not a list")

    return r


# ── Synthetic builders ────────────────────────────────────────────────────────

def build_synthetic_teardown_record(
    *,
    provider: str,
    failure_type: str,
    teardown_state: str | None = None,
    raw_failure_reason: str = "",
    orphan_sweep_triggered: bool = False,
    residual_instance_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal conformant ProviderTeardownRecord."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    exec_uuid = uuid.uuid4().hex[:12]
    is_serverless = provider in SERVERLESS_PROVIDERS
    lane = PROVIDER_LANE_MAP.get(provider, "job")

    if teardown_state is None:
        if is_serverless:
            teardown_state = "not_applicable"
        elif failure_type == "user_cancelled":
            teardown_state = "completed"
        else:
            teardown_state = "completed"

    destroy_attempted = not is_serverless
    destroy_succeeded = destroy_attempted and teardown_state == "completed"

    return {
        "execution_id": f"sp05-{provider}-{exec_uuid}",
        "provider": provider,
        "provider_job_id": f"pjob-{exec_uuid}",
        "provider_instance_id": f"pinst-{exec_uuid}",
        "lane": lane,
        "failure_type": failure_type,
        "teardown_state": teardown_state,
        "destroy_attempted": destroy_attempted,
        "destroy_succeeded": destroy_succeeded,
        "storage_authority": COMPUTE_STORAGE_AUTHORITY,
        "started_at": now,
        "finished_at": now,
        "orphan_sweep_triggered": orphan_sweep_triggered,
        "residual_instance_ids": list(residual_instance_ids or []),
        "raw_failure_reason": (
            raw_failure_reason
            if failure_type in FAILURE_TYPES_REQUIRING_REASON
            else ""
        ),
    }


def build_synthetic_orphan_sweep_record(
    *,
    provider: str,
    instances_scanned: int = 10,
    orphans_detected: int = 1,
    orphans_destroyed: int = 1,
    orphans_preserved: int = 0,
    sweep_status: str = "completed",
    failure_details: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal conformant OrphanSweepRecord."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    sweep_id = f"sweep-{provider}-{uuid.uuid4().hex[:10]}"
    return {
        "sweep_id": sweep_id,
        "provider": provider,
        "sweep_triggered_at": now,
        "sweep_completed_at": now,
        "instances_scanned": instances_scanned,
        "orphans_detected": orphans_detected,
        "orphans_destroyed": orphans_destroyed,
        "orphans_preserved": orphans_preserved,
        "sweep_status": sweep_status,
        "evidence_uri": "",
        "failure_details": list(failure_details or []),
    }


# ── Proof runner ──────────────────────────────────────────────────────────────

def run_provider_teardown_proof() -> dict[str, Any]:
    """Run the SP-05 provider teardown and orphan control proof.

    Tests (26 steps):
      Phase 1: All 3 providers × all 8 failure types → conformant teardown records
      Phase 2: Orphan sweep records for all 3 providers
      Phase 3: Rejection tests (8 cases)

    Returns:
        Structured JSON-serialisable proof manifest.
    """
    seed_env_from_dotenv()

    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    steps: list[dict[str, Any]] = []

    def _step(name: str, ok: bool, detail: str, errors: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"step": name, "ok": ok, "detail": detail}
        if errors:
            entry["errors"] = errors
        steps.append(entry)

    # ── Phase 1: providers × failure types ────────────────────────────────────
    for provider in sorted(PROVIDER_REGISTRY):
        for failure_type in sorted(PROVIDER_FAILURE_TAXONOMY):
            raw_reason = (
                f"synthetic_{failure_type}_from_{provider}"
                if failure_type in FAILURE_TYPES_REQUIRING_REASON
                else ""
            )
            record = build_synthetic_teardown_record(
                provider=provider,
                failure_type=failure_type,
                raw_failure_reason=raw_reason,
            )
            result = validate_provider_teardown_record(record)
            _step(
                f"{provider}.{failure_type}",
                result.ok,
                "conformant" if result.ok else f"errors={result.errors}",
                result.errors if not result.ok else None,
            )

    # ── Phase 2: orphan sweep records for each provider ───────────────────────
    for provider in sorted(PROVIDER_REGISTRY):
        sweep = build_synthetic_orphan_sweep_record(
            provider=provider,
            instances_scanned=12,
            orphans_detected=2,
            orphans_destroyed=2,
            orphans_preserved=0,
        )
        result = validate_orphan_sweep_record(sweep)
        _step(
            f"orphan_sweep.{provider}",
            result.ok,
            "conformant" if result.ok else f"errors={result.errors}",
            result.errors if not result.ok else None,
        )

    # ── Phase 3: rejection tests ──────────────────────────────────────────────

    # 3a: unknown provider
    bad_provider = build_synthetic_teardown_record(
        provider="vast", failure_type="oom_killed", raw_failure_reason="OOM"
    )
    bad_provider["provider"] = "unknown_cloud"
    r = validate_provider_teardown_record(bad_provider)
    _step(
        "reject_unknown_provider",
        not r.ok,
        f"correctly rejected unknown provider: {r.errors}" if not r.ok
        else "FAIL — unknown provider not rejected",
    )

    # 3b: unknown failure type
    bad_ft = build_synthetic_teardown_record(
        provider="vast", failure_type="provider_evicted",
        raw_failure_reason="spot reclaimed"
    )
    bad_ft["failure_type"] = "mystery_error"
    r2 = validate_provider_teardown_record(bad_ft)
    _step(
        "reject_unknown_failure_type",
        not r2.ok,
        f"correctly rejected unknown failure_type: {r2.errors}" if not r2.ok
        else "FAIL — unknown failure_type not rejected",
    )

    # 3c: missing raw_failure_reason for oom_killed
    bad_reason = build_synthetic_teardown_record(
        provider="vast", failure_type="oom_killed", raw_failure_reason=""
    )
    r3 = validate_provider_teardown_record(bad_reason)
    _step(
        "reject_missing_raw_failure_reason",
        not r3.ok,
        f"correctly rejected missing raw_failure_reason: {r3.errors}" if not r3.ok
        else "FAIL — missing reason not rejected",
    )

    # 3d: wrong storage authority
    bad_auth = build_synthetic_teardown_record(
        provider="runpod", failure_type="user_cancelled"
    )
    bad_auth["storage_authority"] = "wrong-authority"
    r4 = validate_provider_teardown_record(bad_auth)
    _step(
        "reject_wrong_storage_authority",
        not r4.ok,
        f"correctly rejected wrong storage_authority: {r4.errors}" if not r4.ok
        else "FAIL — wrong authority not rejected",
    )

    # 3e: missing timestamps
    bad_ts = build_synthetic_teardown_record(
        provider="modal", failure_type="provider_api_error",
        raw_failure_reason="500 Internal"
    )
    bad_ts["started_at"] = ""
    bad_ts["finished_at"] = ""
    r5 = validate_provider_teardown_record(bad_ts)
    _step(
        "reject_missing_timestamps",
        not r5.ok,
        f"correctly rejected missing timestamps: {r5.errors}" if not r5.ok
        else "FAIL — missing timestamps not rejected",
    )

    # 3f: invalid teardown_state
    bad_ts2 = build_synthetic_teardown_record(
        provider="vast", failure_type="network_partition",
        raw_failure_reason="connection lost"
    )
    bad_ts2["teardown_state"] = "invented_state"
    r6 = validate_provider_teardown_record(bad_ts2)
    _step(
        "reject_invalid_teardown_state",
        not r6.ok,
        f"correctly rejected invalid teardown_state: {r6.errors}" if not r6.ok
        else "FAIL — invalid teardown_state not rejected",
    )

    # 3g: missing orphan_sweep_triggered
    bad_sweep = build_synthetic_teardown_record(
        provider="runpod", failure_type="user_cancelled"
    )
    bad_sweep.pop("orphan_sweep_triggered", None)
    r7 = validate_provider_teardown_record(bad_sweep)
    _step(
        "reject_missing_orphan_sweep_triggered",
        not r7.ok,
        f"correctly rejected missing orphan_sweep_triggered: {r7.errors}" if not r7.ok
        else "FAIL — missing field not rejected",
    )

    # 3h: orphan sweep bad sweep_status
    bad_sw_rec = build_synthetic_orphan_sweep_record(
        provider="vast", sweep_status="unknown_status"
    )
    r8 = validate_orphan_sweep_record(bad_sw_rec)
    _step(
        "reject_invalid_sweep_status",
        not r8.ok,
        f"correctly rejected invalid sweep_status: {r8.errors}" if not r8.ok
        else "FAIL — invalid sweep_status not rejected",
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    all_ok = all(s["ok"] for s in steps)
    passed_count = sum(1 for s in steps if s["ok"])
    final_status = "pass" if all_ok else "blocked"

    provider_failure_combos = len(PROVIDER_REGISTRY) * len(PROVIDER_FAILURE_TAXONOMY)
    orphan_sweep_steps = len(PROVIDER_REGISTRY)

    return {
        "program": "SP-05-PROVIDER_TEARDOWN_AND_ORPHAN_CONTROL",
        "decision": {
            "status": final_status,
            "teardown_control_unified": all_ok,
            "reason_code": "ok" if all_ok else "provider_teardown_contract_violation",
            "remediation_hint": (
                ""
                if all_ok
                else "One or more provider teardown or orphan sweep assertions failed. "
                "Review PROVIDER_FAILURE_TAXONOMY, validate_provider_teardown_record(), "
                "and validate_orphan_sweep_record() for the failing provider/failure_type."
            ),
        },
        "policy": {
            "provider_registry": sorted(PROVIDER_REGISTRY),
            "provider_lane_map": PROVIDER_LANE_MAP,
            "serverless_providers": sorted(SERVERLESS_PROVIDERS),
            "failure_taxonomy": sorted(PROVIDER_FAILURE_TAXONOMY),
            "failure_types_requiring_reason": sorted(FAILURE_TYPES_REQUIRING_REASON),
            "teardown_states": sorted(TEARDOWN_STATES),
            "sweep_states": sorted(SWEEP_STATES),
            "orphan_staleness_threshold_seconds": ORPHAN_STALENESS_THRESHOLD_S,
            "storage_authority": COMPUTE_STORAGE_AUTHORITY,
        },
        "steps": steps,
        "summary": {
            "steps_total": len(steps),
            "steps_passed": passed_count,
            "steps_failed": len(steps) - passed_count,
            "all_phases_ok": all_ok,
            "provider_failure_combos_tested": provider_failure_combos,
            "orphan_sweep_steps": orphan_sweep_steps,
            "rejection_tests": 8,
        },
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
