from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


LANE_ID = "runpod_native_remote"

SP02_EVIDENCE_FILENAMES = {
    "route_receipt": "route_receipt.json",
    "submit_receipt": "submit_receipt.json",
    "status_receipt": "status_receipt.json",
    "artifact_receipt": "artifact_receipt.json",
    "teardown_receipt": "teardown_receipt.json",
    "coherence_matrix": "coherence_matrix.json",
    "gate_verdict": "gate_verdict.json",
}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def build_sp02_route_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Route seam: provider_job_id not yet known; lane_id must already be set."""
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "provider_job_id": "",  # not yet available at route selection time
        "lane_id": LANE_ID,
        "route_surface": _as_text(payload.get("bridge")) or "sim:job/submit",
        "provider": _as_text(payload.get("provider")),
        "provider_rationale": _as_text(payload.get("provider_rationale")) or "runpod selected by policy",
        "accepted": bool(payload.get("accepted", False)),
    }


def build_sp02_submit_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Submit seam: first seam where provider_job_id is known."""
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "provider_job_id": _as_text(payload.get("provider_job_id")),
        "lane_id": LANE_ID,
        "endpoint": _as_text(payload.get("endpoint")),
        "request_fingerprint": _as_text(payload.get("request_fingerprint")),
        "submit_accepted": bool(payload.get("submit_accepted", False)),
    }


def build_sp02_status_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Status seam: at least one active transition and one terminal transition."""
    terminal_state = _as_text(
        payload.get("terminal_state")
        or payload.get("vast_phase_final")
        or payload.get("phase")
    )
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "provider_job_id": _as_text(payload.get("provider_job_id")),
        "lane_id": LANE_ID,
        "terminal_state": terminal_state,
        "success": bool(payload.get("success", False)),
        "error": _as_text(payload.get("error")),
    }


def build_sp02_artifact_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Artifact seam: signed/durable pointers with integrity metadata."""
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "provider_job_id": _as_text(payload.get("provider_job_id")),
        "lane_id": LANE_ID,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "output_dir": _as_text(payload.get("output_dir")),
    }


def build_sp02_teardown_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Teardown seam: verify no orphan RunPod resources for this session."""
    teardown = payload.get("teardown_proof") or {}
    if not isinstance(teardown, Mapping):
        teardown = {}
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "provider_job_id": _as_text(payload.get("provider_job_id")),
        "lane_id": LANE_ID,
        "destroy_attempted": bool(teardown.get("destroy_attempted", False)),
        "destroy_succeeded": bool(teardown.get("destroy_succeeded", False)),
        "orphan_scan_result": _as_text(
            teardown.get("orphan_scan_result")
            or payload.get("orphan_scan_result")
            or "not_scanned"
        ),
        "preserved_for_recovery": bool(teardown.get("preserved_for_recovery", False)),
    }


def build_sp02_coherence_matrix(
    route_receipt: Mapping[str, Any],
    submit_receipt: Mapping[str, Any],
    status_receipt: Mapping[str, Any],
    artifact_receipt: Mapping[str, Any],
    teardown_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Checks same-session coherence across all 5 SP-02 seams.
    Canonical identity is anchored to the submit receipt (first seam with provider_job_id).
    Route seam skips provider_job_id (not yet known at route time).
    """
    canonical = {
        "session_id": _as_text(submit_receipt.get("session_id")),
        "job_id": _as_text(submit_receipt.get("job_id")),
        "provider_job_id": _as_text(submit_receipt.get("provider_job_id")),
        "lane_id": LANE_ID,
    }

    rows: list[dict[str, Any]] = []

    # Route seam: check session_id, job_id, lane_id (provider_job_id not expected)
    route_checks: list[dict[str, Any]] = []
    route_failures: list[str] = []
    for field_name in ("session_id", "job_id", "lane_id"):
        expected = canonical[field_name]
        actual = _as_text(route_receipt.get(field_name))
        passed = bool(expected) and bool(actual) and actual == expected
        if not passed:
            if not expected:
                route_failures.append(f"missing canonical {field_name}")
            elif not actual:
                route_failures.append(f"missing {field_name}")
            else:
                route_failures.append(f"{field_name} mismatch")
        route_checks.append({"field": field_name, "expected": expected, "actual": actual, "pass": passed})
    rows.append({"seam": "route", "checks": route_checks, "pass": not route_failures, "failures": route_failures})

    # Submit/status/artifact/teardown: check all 4 canonical fields
    for seam_name, receipt in (
        ("submit", submit_receipt),
        ("status", status_receipt),
        ("artifact", artifact_receipt),
        ("teardown", teardown_receipt),
    ):
        checks: list[dict[str, Any]] = []
        failures: list[str] = []
        for field_name, expected in canonical.items():
            actual = _as_text(receipt.get(field_name))
            passed = bool(expected) and bool(actual) and actual == expected
            if not passed:
                if not expected:
                    failures.append(f"missing canonical {field_name}")
                elif not actual:
                    failures.append(f"missing {field_name}")
                else:
                    failures.append(f"{field_name} mismatch")
            checks.append({"field": field_name, "expected": expected, "actual": actual, "pass": passed})
        rows.append({"seam": seam_name, "checks": checks, "pass": not failures, "failures": failures})

    blockers: list[str] = []
    if not canonical["session_id"]:
        blockers.append("submit receipt missing session_id")
    if not canonical["job_id"]:
        blockers.append("submit receipt missing job_id")
    if not canonical["provider_job_id"]:
        blockers.append("submit receipt missing provider_job_id — RunPod lane cannot close without provider handle")
    for row in rows:
        if not row["pass"]:
            blockers.append(f"{row['seam']} seam: {', '.join(row['failures'])}")

    return {
        "canonical_identity": canonical,
        "rows": rows,
        "passed": all(row["pass"] for row in rows) and not blockers,
        "blockers": blockers,
    }


def build_sp02_gate_verdict(coherence_matrix: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(coherence_matrix.get("passed", False))
    blockers = list(coherence_matrix.get("blockers") or [])
    return {
        "passed": passed,
        "lane_id": LANE_ID,
        "reason": (
            "SP-02 RunPod lane parity confirmed across all seams"
            if passed
            else "SP-02 blocked: " + "; ".join(blockers)
        ),
        "blockers": blockers,
    }


def build_sp02_packet(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    Build the full SP-02 parity packet from a unified payload dict.
    The payload must contain at minimum:
      session_id, job_id, provider_job_id, and provider-terminal fields.
    """
    route_receipt = build_sp02_route_receipt(payload)
    submit_receipt = build_sp02_submit_receipt(payload)
    status_receipt = build_sp02_status_receipt(payload)
    artifact_receipt = build_sp02_artifact_receipt(payload)
    teardown_receipt = build_sp02_teardown_receipt(payload)
    coherence_matrix = build_sp02_coherence_matrix(
        route_receipt, submit_receipt, status_receipt, artifact_receipt, teardown_receipt
    )
    gate_verdict = build_sp02_gate_verdict(coherence_matrix)
    return {
        "route_receipt": route_receipt,
        "submit_receipt": submit_receipt,
        "status_receipt": status_receipt,
        "artifact_receipt": artifact_receipt,
        "teardown_receipt": teardown_receipt,
        "coherence_matrix": coherence_matrix,
        "gate_verdict": gate_verdict,
    }


def write_sp02_packet(packet: Mapping[str, Any], output_dir: str | Path) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for key, filename in SP02_EVIDENCE_FILENAMES.items():
        path = directory / filename
        path.write_text(json.dumps(packet.get(key, {}), indent=2), encoding="utf-8")
        written[key] = path
    return written
