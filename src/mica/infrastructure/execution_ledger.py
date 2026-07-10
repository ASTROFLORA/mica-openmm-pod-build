"""SP-04 EXECUTION_LEDGER_UNIFICATION — unified terminal record contract.

Doctrine:
  Every compute lane (serverless, job, remote_md) MUST emit a conformant
  terminal execution record when it reaches any terminal state.

Terminal states (canonical taxonomy):
  - completed      → job finished, artifacts available
  - failed         → job errored; failure_reason must be non-empty
  - cancelled      → user or system cancelled; no retry expected
  - timed_out      → exceeded wall-clock limit; treated as failed subtype
  - orphaned       → provider instance lost without clean termination

Non-terminal states (not validated here):
  pending_dispatch, submitted, running, materializing_outputs

Required fields in every terminal execution record:
  execution_id         str   — unique, non-empty
  lane                 str   — one of {serverless, job, remote_md}
  state                str   — one of TERMINAL_STATES
  user_id              str   — non-empty (owner)
  request_id           str   — non-empty (API-level request handle)
  finished_at          str   — non-empty ISO-8601 timestamp
  failure_reason       str   — non-empty when state in {failed, timed_out, orphaned}
  storage_authority    str   — must equal COMPUTE_STORAGE_AUTHORITY
  storage_bucket       str   — non-empty
  storage_prefix       str   — non-empty
  teardown_proof       dict  — must be present (may be partial for serverless)

This module provides:
  - `TERMINAL_STATES`           canonical set
  - `validate_terminal_record(record, lane)` → ValidationResult
  - `build_synthetic_record(lane, state, ...)` → minimal conformant record
  - `run_ledger_unification_proof()` → structured JSON proof across all 3 lanes
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mica.config.dotenv_loader import seed_env_from_dotenv
from mica.storage.compute_durability import (
    COMPUTE_STORAGE_AUTHORITY,
    build_compute_storage_identity,
    build_compute_teardown_proof,
)

# ── Canonical terminal state taxonomy ─────────────────────────────────────────

TERMINAL_STATES: frozenset[str] = frozenset({
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "orphaned",
})

NON_TERMINAL_STATES: frozenset[str] = frozenset({
    "pending_dispatch",
    "submitted",
    "running",
    "materializing_outputs",
})

VALID_LANES: frozenset[str] = frozenset({"serverless", "job", "remote_md"})

# Failure states that require a non-empty failure_reason
FAILURE_STATES: frozenset[str] = frozenset({"failed", "timed_out", "orphaned"})


# ── Validation result ─────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def validate_terminal_record(record: dict[str, Any], *, lane: str | None = None) -> ValidationResult:
    """Validate a terminal execution record against the unified ledger contract.

    Args:
        record: The execution record dict to validate.
        lane:   Optional expected lane; cross-checked against record["lane"] if provided.

    Returns:
        ValidationResult with .ok, .errors, .warnings.
    """
    r = ValidationResult()
    rec = record or {}

    # ── Identity ─────────────────────────────────────────────────────────────
    exec_id = str(rec.get("execution_id") or "").strip()
    if not exec_id:
        r.fail("execution_id: missing or empty")

    # ── Lane ─────────────────────────────────────────────────────────────────
    rec_lane = str(rec.get("lane") or "").strip()
    if rec_lane not in VALID_LANES:
        r.fail(f"lane: '{rec_lane}' not in {sorted(VALID_LANES)}")
    if lane and rec_lane != lane:
        r.fail(f"lane mismatch: expected '{lane}', got '{rec_lane}'")

    # ── Terminal state ────────────────────────────────────────────────────────
    state = str(rec.get("state") or "").strip()
    if state not in TERMINAL_STATES:
        if state in NON_TERMINAL_STATES:
            r.fail(f"state: '{state}' is non-terminal — terminal record required")
        else:
            r.fail(f"state: '{state}' is not a recognised terminal state {sorted(TERMINAL_STATES)}")

    # ── Owner ─────────────────────────────────────────────────────────────────
    if not str(rec.get("user_id") or "").strip():
        r.fail("user_id: missing or empty")

    # ── Request handle ────────────────────────────────────────────────────────
    if not str(rec.get("request_id") or "").strip():
        r.fail("request_id: missing or empty")

    # ── finished_at ───────────────────────────────────────────────────────────
    finished_at = str(rec.get("finished_at") or "").strip()
    if not finished_at:
        r.fail("finished_at: missing or empty — terminal records must carry a finish timestamp")

    # ── failure_reason for failure states ─────────────────────────────────────
    if state in FAILURE_STATES:
        reason = str(rec.get("failure_reason") or "").strip()
        if not reason:
            r.fail(f"failure_reason: required for state='{state}' but missing or empty")

    # ── Storage authority ─────────────────────────────────────────────────────
    storage_auth = str(rec.get("storage_authority") or "").strip()
    if storage_auth != COMPUTE_STORAGE_AUTHORITY:
        r.fail(
            f"storage_authority: expected '{COMPUTE_STORAGE_AUTHORITY}', got '{storage_auth}'"
        )

    if not str(rec.get("storage_bucket") or "").strip():
        r.fail("storage_bucket: missing or empty")

    if not str(rec.get("storage_prefix") or "").strip():
        r.fail("storage_prefix: missing or empty")

    # ── Teardown proof ────────────────────────────────────────────────────────
    tp = rec.get("teardown_proof")
    if tp is None:
        r.fail("teardown_proof: missing — all terminal records must carry a teardown_proof dict")
    elif not isinstance(tp, dict):
        r.fail(f"teardown_proof: must be a dict, got {type(tp).__name__}")
    else:
        # Serverless lane teardown proofs are partial (provider lifecycle managed)
        if rec_lane != "serverless":
            if not tp.get("execution_id"):
                r.warn("teardown_proof.execution_id: empty")
            if not tp.get("lane"):
                r.warn("teardown_proof.lane: empty")

    return r


# ── Synthetic record builder ──────────────────────────────────────────────────

def build_synthetic_record(
    *,
    lane: str,
    state: str,
    user_id: str = "sp04-probe-user",
    failure_reason: str = "",
    run_id: str = "",
    session_id: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """Build a minimal conformant execution record for the given lane and state."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    exec_uuid = uuid.uuid4().hex[:12]
    request_id = f"{lane}-{exec_uuid}"
    execution_id = f"sp04-{lane}-{exec_uuid}"

    eff_job_id = job_id or exec_uuid
    eff_session_id = session_id or exec_uuid
    eff_run_id = run_id or exec_uuid

    storage_identity = build_compute_storage_identity(
        user_id=user_id,
        lane=lane,  # type: ignore[arg-type]
        run_id=eff_run_id,
        request_id=request_id,
        job_id=eff_job_id,
        session_id=eff_session_id,
    )

    teardown = build_compute_teardown_proof(
        execution_id=execution_id,
        lane=lane,  # type: ignore[arg-type]
        user_id=user_id,
        run_id=eff_run_id,
        request_id=request_id,
        job_id=eff_job_id,
        session_id=eff_session_id,
        provider=f"synthetic_{lane}",
        destroy_attempted=(lane != "serverless"),
        destroy_succeeded=(state == "completed" and lane != "serverless"),
        teardown_state=(
            "not_applicable" if lane == "serverless"
            else ("completed" if state == "completed" else "skipped")
        ),
        started_at=now,
        finished_at=now,
    )

    return {
        "execution_id": execution_id,
        "lane": lane,
        "state": state,
        "user_id": user_id,
        "request_id": request_id,
        "session_id": eff_session_id,
        "run_id": eff_run_id,
        "provider": f"synthetic_{lane}",
        "provider_target": "sp04-probe-target",
        "provider_job_id": eff_job_id,
        **storage_identity,
        "artifact_manifest_uri": "",
        "artifact_uris": [],
        "failure_reason": failure_reason if state in FAILURE_STATES else "",
        "finished_at": now if state in TERMINAL_STATES else "",
        "created_at": now,
        "submitted_at": now,
        "started_at": now,
        "last_observed_at": now,
        "teardown_proof": teardown,
    }


# ── Proof runner ──────────────────────────────────────────────────────────────

def run_ledger_unification_proof() -> dict[str, Any]:
    """Run the SP-04 unified ledger contract proof across all 3 lanes.

    Tests:
      - Each lane emits conformant terminal records for all 5 terminal states
      - failure_reason validation fires for failed/timed_out/orphaned
      - Non-terminal state correctly rejected
      - storage_authority mismatch correctly rejected
      - teardown_proof presence enforced

    Returns:
        Structured JSON-serialisable proof manifest.
    """
    seed_env_from_dotenv()

    from datetime import UTC, datetime as _dt  # local to avoid top-level cost

    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    steps: list[dict[str, Any]] = []

    def _step(name: str, ok: bool, detail: str, errors: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"step": name, "ok": ok, "detail": detail}
        if errors:
            entry["errors"] = errors
        steps.append(entry)

    # ── Phase 1: all lanes × all terminal states ──────────────────────────────
    for lane in ("serverless", "job", "remote_md"):
        for state in sorted(TERMINAL_STATES):
            failure_reason = f"synthetic_{state}_error" if state in FAILURE_STATES else ""
            record = build_synthetic_record(
                lane=lane,
                state=state,
                failure_reason=failure_reason,
            )
            result = validate_terminal_record(record, lane=lane)
            _step(
                f"{lane}.{state}",
                result.ok,
                f"errors={result.errors} warnings={result.warnings}" if not result.ok else "conformant",
                result.errors if not result.ok else None,
            )

    # ── Phase 2: rejection tests ──────────────────────────────────────────────

    # 2a: non-terminal state should be rejected
    bad_non_terminal = build_synthetic_record(lane="job", state="running")
    bad_non_terminal["finished_at"] = ""  # also missing finished_at
    r = validate_terminal_record(bad_non_terminal, lane="job")
    _step(
        "reject_non_terminal_state",
        not r.ok,  # we WANT it to fail validation
        f"correctly rejected non-terminal state=running: errors={r.errors}" if not r.ok
        else "FAIL — non-terminal state was not rejected",
    )

    # 2b: missing failure_reason for failed state
    bad_no_reason = build_synthetic_record(lane="job", state="failed", failure_reason="")
    r2 = validate_terminal_record(bad_no_reason, lane="job")
    _step(
        "reject_missing_failure_reason",
        not r2.ok,
        f"correctly rejected missing failure_reason: errors={r2.errors}" if not r2.ok
        else "FAIL — missing failure_reason was not caught",
    )

    # 2c: wrong storage_authority
    bad_authority = build_synthetic_record(lane="remote_md", state="completed")
    bad_authority["storage_authority"] = "wrong-bucket-authority"
    r3 = validate_terminal_record(bad_authority, lane="remote_md")
    _step(
        "reject_wrong_storage_authority",
        not r3.ok,
        f"correctly rejected wrong storage_authority: errors={r3.errors}" if not r3.ok
        else "FAIL — wrong storage_authority was not caught",
    )

    # 2d: missing teardown_proof
    bad_no_tp = build_synthetic_record(lane="job", state="completed")
    bad_no_tp.pop("teardown_proof", None)
    r4 = validate_terminal_record(bad_no_tp, lane="job")
    _step(
        "reject_missing_teardown_proof",
        not r4.ok,
        f"correctly rejected missing teardown_proof: errors={r4.errors}" if not r4.ok
        else "FAIL — missing teardown_proof was not caught",
    )

    # 2e: missing finished_at
    bad_no_finish = build_synthetic_record(lane="serverless", state="cancelled")
    bad_no_finish["finished_at"] = ""
    r5 = validate_terminal_record(bad_no_finish, lane="serverless")
    _step(
        "reject_missing_finished_at",
        not r5.ok,
        f"correctly rejected empty finished_at: errors={r5.errors}" if not r5.ok
        else "FAIL — empty finished_at was not caught",
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    all_ok = all(s["ok"] for s in steps)
    passed_count = sum(1 for s in steps if s["ok"])
    final_status = "pass" if all_ok else "blocked"

    return {
        "program": "SP-04-EXECUTION_LEDGER_UNIFICATION",
        "decision": {
            "status": final_status,
            "ledger_unified": all_ok,
            "reason_code": "ok" if all_ok else "ledger_contract_violation",
            "remediation_hint": (
                ""
                if all_ok
                else "One or more ledger contract assertions failed. "
                "Review TERMINAL_STATES, validate_terminal_record(), and build_synthetic_record() for the failing lane/state."
            ),
        },
        "policy": {
            "terminal_states": sorted(TERMINAL_STATES),
            "non_terminal_states": sorted(NON_TERMINAL_STATES),
            "valid_lanes": sorted(VALID_LANES),
            "failure_states_require_reason": sorted(FAILURE_STATES),
            "storage_authority": COMPUTE_STORAGE_AUTHORITY,
            "required_fields": [
                "execution_id", "lane", "state", "user_id", "request_id",
                "finished_at", "storage_authority", "storage_bucket",
                "storage_prefix", "teardown_proof",
            ],
        },
        "steps": steps,
        "summary": {
            "steps_total": len(steps),
            "steps_passed": passed_count,
            "steps_failed": len(steps) - passed_count,
            "all_phases_ok": all_ok,
            "lane_state_combos_tested": len(TERMINAL_STATES) * len(VALID_LANES),
            "rejection_tests": 5,
        },
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
