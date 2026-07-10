"""SP-03 SANDBOX_BOUNDARY_HARDENING — sandbox context classifier and enforcement guard.

Doctrine:
  - Modal sandbox (or any sandbox backend) is a disposable compute resource.
  - Sandbox executions MUST NOT produce durable compute authority records.
  - Specifically blocked from sandbox context:
      * lane=remote_md teardown proofs
      * canonical_compute_storage_prefix(lane=remote_md/job) writes
      * md-jobs/ or jobs/ GCS prefix claims
  - Sandbox context may only claim lane=serverless or lane=sandbox (probe/temp).

Context detection signals (in priority order):
  1. MICA_SANDBOX_CONTEXT=modal|local|sandbox   (explicit override)
  2. MODAL_SANDBOX_ID                           (set by Modal inside sandboxes)
  3. MICA_EXECUTION_LANE=sandbox                (explicit lane override)

Enforcement:
  - `classify_execution_context()` → structured context dict (pure read, no side-effects)
  - `assert_not_sandbox_for_lane(lane)` → raises SandboxBoundaryViolation if calling
    from sandbox with a durable production lane (remote_md / job)
  - `run_sandbox_boundary_proof()` → structured JSON proof that exercises all
    boundary assertions deterministically (no network required)

This module is intentionally import-safe and dependency-light — it must be usable
from inside a compute node or sandbox runner without pulling the full mica stack.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from mica.config.dotenv_loader import seed_env_from_dotenv

# ── Durable production lanes that sandbox is NOT allowed to claim ─────────────

DURABLE_LANES: frozenset[str] = frozenset({"remote_md", "job"})

# ── GCS prefixes that sandbox writes must NOT produce ─────────────────────────

BLOCKED_SANDBOX_GCS_ROOTS: frozenset[str] = frozenset({"md-jobs", "jobs"})

# ── Allowed lane for sandbox-context records ──────────────────────────────────

SANDBOX_ALLOWED_LANE: str = "serverless"


class SandboxBoundaryViolation(RuntimeError):
    """Raised when sandbox context attempts to claim a durable production lane."""

    def __init__(self, lane: str, context: str, signals: list[str]) -> None:
        self.lane = lane
        self.context = context
        self.signals = signals
        super().__init__(
            f"SANDBOX BOUNDARY VIOLATION: lane='{lane}' is a durable production lane "
            f"but execution context is '{context}' (signals={signals}). "
            f"Sandbox executions may only claim lane='{SANDBOX_ALLOWED_LANE}'."
        )


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def classify_execution_context() -> dict[str, Any]:
    """Classify the current execution context without side-effects.

    Returns a dict:
        context:     "sandbox" | "host"
        signals:     list[str] — which env signals triggered the classification
        lane_authority: "serverless" | "remote_md" | "job" | "unrestricted"
        is_sandbox:  bool
        details:     dict with raw env values inspected
    """
    signals: list[str] = []
    details: dict[str, Any] = {}

    # Signal 1: explicit MICA_SANDBOX_CONTEXT
    mica_ctx = os.getenv("MICA_SANDBOX_CONTEXT", "").strip().lower()
    details["MICA_SANDBOX_CONTEXT"] = mica_ctx or None
    if mica_ctx in ("modal", "local_sandbox", "sandbox"):
        signals.append(f"MICA_SANDBOX_CONTEXT={mica_ctx}")

    # Signal 2: Modal-injected MODAL_SANDBOX_ID
    modal_id = os.getenv("MODAL_SANDBOX_ID", "").strip()
    details["MODAL_SANDBOX_ID"] = modal_id or None
    if modal_id:
        signals.append(f"MODAL_SANDBOX_ID={modal_id[:16]}...")

    # Signal 3: explicit MICA_EXECUTION_LANE=sandbox
    exec_lane = os.getenv("MICA_EXECUTION_LANE", "").strip().lower()
    details["MICA_EXECUTION_LANE"] = exec_lane or None
    if exec_lane == "sandbox":
        signals.append(f"MICA_EXECUTION_LANE={exec_lane}")

    is_sandbox = bool(signals)
    context = "sandbox" if is_sandbox else "host"

    if is_sandbox:
        lane_authority = SANDBOX_ALLOWED_LANE
    else:
        # On host, lane authority comes from MICA_EXECUTION_LANE (if set and valid)
        if exec_lane in DURABLE_LANES:
            lane_authority = exec_lane
        else:
            lane_authority = "unrestricted"

    return {
        "context": context,
        "is_sandbox": is_sandbox,
        "signals": signals,
        "lane_authority": lane_authority,
        "details": details,
    }


def assert_not_sandbox_for_lane(lane: str) -> None:
    """Raise SandboxBoundaryViolation if in sandbox context and lane is durable.

    Call this from any path that would emit a teardown proof, write to a
    canonical compute GCS prefix, or create a durable execution record.

    Args:
        lane: The compute lane being claimed ("remote_md", "job", "serverless").

    Raises:
        SandboxBoundaryViolation: If in sandbox context and lane is durable.
    """
    if lane not in DURABLE_LANES:
        return  # serverless or unknown — not a protected lane
    ctx = classify_execution_context()
    if ctx["is_sandbox"]:
        raise SandboxBoundaryViolation(
            lane=lane,
            context=ctx["context"],
            signals=ctx["signals"],
        )


def probe_gcs_prefix_blocked(prefix_root: str) -> bool:
    """Return True if the GCS prefix root is blocked for sandbox context.

    Does NOT raise — pure predicate for logging and manifest building.
    """
    ctx = classify_execution_context()
    if not ctx["is_sandbox"]:
        return False
    return prefix_root in BLOCKED_SANDBOX_GCS_ROOTS


def run_sandbox_boundary_proof(
    *,
    simulate_sandbox: bool = True,
) -> dict[str, Any]:
    """Run all SP-03 boundary assertions and return structured evidence.

    This is deterministic — no network calls, no GCS access.

    Args:
        simulate_sandbox: If True, temporarily sets env vars to simulate
            sandbox context for assertion testing. If False, uses current env.

    Returns:
        Structured JSON-serialisable proof manifest.
    """
    seed_env_from_dotenv()

    started_at = _utcnow()
    steps: list[dict[str, Any]] = []
    final_status = "blocked"

    # ── Step 0: baseline context (host mode) ─────────────────────────────────
    host_ctx = classify_execution_context()
    steps.append({
        "step": "baseline_context",
        "ok": True,
        "context": host_ctx["context"],
        "is_sandbox": host_ctx["is_sandbox"],
        "signals": host_ctx["signals"],
        "detail": f"host execution context confirmed: is_sandbox={host_ctx['is_sandbox']}",
    })

    # ── Step 1: simulate sandbox context and classify ─────────────────────────
    if simulate_sandbox:
        _prev = {
            "MICA_SANDBOX_CONTEXT": os.environ.get("MICA_SANDBOX_CONTEXT"),
            "MODAL_SANDBOX_ID": os.environ.get("MODAL_SANDBOX_ID"),
        }
        os.environ["MICA_SANDBOX_CONTEXT"] = "modal"
        os.environ["MODAL_SANDBOX_ID"] = "sp03-probe-sandbox-id-0000"

    try:
        sandbox_ctx = classify_execution_context()
        sandbox_detected = sandbox_ctx["is_sandbox"]
        steps.append({
            "step": "sandbox_context_detection",
            "ok": sandbox_detected,
            "context": sandbox_ctx["context"],
            "is_sandbox": sandbox_detected,
            "signals": sandbox_ctx["signals"],
            "lane_authority": sandbox_ctx["lane_authority"],
            "detail": f"sandbox signals detected: {sandbox_ctx['signals']}",
        })

        if not sandbox_detected:
            raise RuntimeError("Sandbox context simulation failed — no signals detected.")

        # ── Step 2: assert remote_md lane is BLOCKED in sandbox ───────────────
        try:
            assert_not_sandbox_for_lane("remote_md")
            steps.append({
                "step": "block_remote_md_lane",
                "ok": False,
                "detail": "FAIL — assert_not_sandbox_for_lane('remote_md') did NOT raise — boundary not enforced",
            })
        except SandboxBoundaryViolation as exc:
            steps.append({
                "step": "block_remote_md_lane",
                "ok": True,
                "detail": f"BLOCKED as expected: {str(exc)[:200]}",
                "violation_lane": exc.lane,
                "violation_signals": exc.signals,
            })

        # ── Step 3: assert job lane is BLOCKED in sandbox ─────────────────────
        try:
            assert_not_sandbox_for_lane("job")
            steps.append({
                "step": "block_job_lane",
                "ok": False,
                "detail": "FAIL — assert_not_sandbox_for_lane('job') did NOT raise — boundary not enforced",
            })
        except SandboxBoundaryViolation as exc:
            steps.append({
                "step": "block_job_lane",
                "ok": True,
                "detail": f"BLOCKED as expected: {str(exc)[:200]}",
                "violation_lane": exc.lane,
            })

        # ── Step 4: assert serverless lane is ALLOWED in sandbox ──────────────
        try:
            assert_not_sandbox_for_lane("serverless")
            steps.append({
                "step": "allow_serverless_lane",
                "ok": True,
                "detail": "ALLOWED as expected: serverless is the permitted sandbox lane",
            })
        except SandboxBoundaryViolation as exc:
            steps.append({
                "step": "allow_serverless_lane",
                "ok": False,
                "detail": f"UNEXPECTED BLOCK: serverless should be allowed but got {exc}",
            })

        # ── Step 5: GCS prefix block check ───────────────────────────────────
        md_jobs_blocked = probe_gcs_prefix_blocked("md-jobs")
        jobs_blocked = probe_gcs_prefix_blocked("jobs")
        serverless_not_blocked = not probe_gcs_prefix_blocked("serverless")
        steps.append({
            "step": "gcs_prefix_block",
            "ok": md_jobs_blocked and jobs_blocked and serverless_not_blocked,
            "md_jobs_blocked": md_jobs_blocked,
            "jobs_blocked": jobs_blocked,
            "serverless_allowed": serverless_not_blocked,
            "detail": (
                f"md-jobs blocked={md_jobs_blocked}, jobs blocked={jobs_blocked}, "
                f"serverless allowed={serverless_not_blocked}"
            ),
        })

        # ── Step 6: teardown proof guard (simulate build attempt) ─────────────
        teardown_blocked = False
        teardown_detail = ""
        try:
            assert_not_sandbox_for_lane("remote_md")
            # If we reach here, teardown proof would have been emitted — that's wrong
            teardown_blocked = False
            teardown_detail = "FAIL — teardown proof would have been emitted from sandbox"
        except SandboxBoundaryViolation:
            teardown_blocked = True
            teardown_detail = "BLOCKED — teardown proof cannot be emitted from sandbox context"

        steps.append({
            "step": "teardown_proof_guard",
            "ok": teardown_blocked,
            "teardown_proof_blocked": teardown_blocked,
            "detail": teardown_detail,
        })

    finally:
        # ── Restore env ───────────────────────────────────────────────────────
        if simulate_sandbox:
            for key, val in _prev.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

    # ── Step 7: confirm host context restored ─────────────────────────────────
    restored_ctx = classify_execution_context()
    steps.append({
        "step": "env_restore_host",
        "ok": not restored_ctx["is_sandbox"],
        "context": restored_ctx["context"],
        "is_sandbox": restored_ctx["is_sandbox"],
        "detail": f"env restored to host context: is_sandbox={restored_ctx['is_sandbox']}",
    })

    # ── Summary ───────────────────────────────────────────────────────────────
    all_ok = all(s["ok"] for s in steps)
    final_status = "pass" if all_ok else "blocked"
    passed_count = sum(1 for s in steps if s["ok"])

    return {
        "program": "SP-03-SANDBOX_BOUNDARY_HARDENING",
        "decision": {
            "status": final_status,
            "boundaries_enforced": all_ok,
            "reason_code": "ok" if all_ok else "boundary_enforcement_failure",
            "remediation_hint": (
                ""
                if all_ok
                else "One or more boundary assertions failed. "
                "Review DURABLE_LANES and assert_not_sandbox_for_lane() guard paths."
            ),
        },
        "policy": {
            "durable_lanes_blocked_in_sandbox": sorted(DURABLE_LANES),
            "sandbox_allowed_lane": SANDBOX_ALLOWED_LANE,
            "blocked_gcs_roots": sorted(BLOCKED_SANDBOX_GCS_ROOTS),
            "detection_signals": [
                "MICA_SANDBOX_CONTEXT in {modal, local_sandbox, sandbox}",
                "MODAL_SANDBOX_ID (set by Modal runtime)",
                "MICA_EXECUTION_LANE=sandbox",
            ],
        },
        "steps": steps,
        "summary": {
            "steps_total": len(steps),
            "steps_passed": passed_count,
            "steps_failed": len(steps) - passed_count,
            "all_phases_ok": all_ok,
        },
        "started_at": started_at,
        "finished_at": _utcnow(),
    }
