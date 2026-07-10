from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SP01_EVIDENCE_FILENAMES = {
    "route_receipt": "route_receipt.json",
    "provider_receipt": "provider_receipt.json",
    "durability_receipt": "durability_receipt.json",
    "coherence_matrix": "coherence_matrix.json",
    "gate_verdict": "gate_verdict.json",
}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _identity_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "route_decision_id": _as_text(payload.get("route_decision_id")),
    }


def build_sp01_route_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "route_decision_id": _as_text(payload.get("route_decision_id")),
        "route_surface": _as_text(payload.get("bridge")) or "sim:job/submit",
        "provider": _as_text(payload.get("provider")),
        "status": _as_text(payload.get("status")),
        "accepted": bool(payload.get("accepted", False)),
        "retryable": bool(payload.get("retryable", False)),
        "status_checked_at": payload.get("status_checked_at"),
        "error": _as_text(payload.get("error")),
        "next_action": _as_text(payload.get("next_action")),
    }


def build_sp01_provider_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "route_decision_id": _as_text(payload.get("route_decision_id")),
        "provider": _as_text(payload.get("provider")),
        "bridge": _as_text(payload.get("bridge")),
        "instance_id": _as_text(payload.get("instance_id")),
        "unified_job_id": _as_text(payload.get("unified_job_id")),
        "unified_provider": _as_text(payload.get("unified_provider")),
        "unified_state": _as_text(payload.get("unified_state")),
        "unified_phase": _as_text(payload.get("unified_phase")),
        "accepted": bool(payload.get("accepted", False)),
        "retryable": bool(payload.get("retryable", False)),
    }


def build_sp01_durability_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    artifact_manifest = payload.get("artifact_manifest")
    if not isinstance(artifact_manifest, Mapping):
        artifact_manifest = {}
    durability_receipt = payload.get("durability_receipt")
    if not isinstance(durability_receipt, Mapping):
        durability_receipt = {}
    return {
        "session_id": _as_text(payload.get("session_id")),
        "job_id": _as_text(payload.get("job_id")),
        "route_decision_id": _as_text(payload.get("route_decision_id")),
        "status": _as_text(payload.get("status")),
        "artifacts": list(payload.get("artifacts") or []),
        "artifact_count": int(payload.get("artifact_count") or 0),
        "artifact_manifest": dict(artifact_manifest),
        "durability_receipt": dict(durability_receipt),
        "status_checked_at": payload.get("status_checked_at"),
        "terminal": bool(payload.get("terminal", False)),
    }


def build_sp01_coherence_matrix(
    route_receipt: Mapping[str, Any],
    provider_receipt: Mapping[str, Any],
    durability_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    canonical = _identity_fields(route_receipt)
    rows: list[dict[str, Any]] = []
    for seam_name, receipt in (
        ("route", route_receipt),
        ("provider", provider_receipt),
        ("durability", durability_receipt),
    ):
        actual = _identity_fields(receipt)
        checks = []
        failures: list[str] = []
        for field_name, expected_value in canonical.items():
            actual_value = actual.get(field_name, "")
            passed = bool(expected_value) and bool(actual_value) and actual_value == expected_value
            if not passed:
                if not expected_value:
                    failures.append(f"missing canonical {field_name}")
                elif not actual_value:
                    failures.append(f"missing {field_name}")
                else:
                    failures.append(f"{field_name} mismatch")
            checks.append(
                {
                    "field": field_name,
                    "expected": expected_value,
                    "actual": actual_value,
                    "pass": passed,
                }
            )
        rows.append(
            {
                "seam": seam_name,
                "identity": actual,
                "checks": checks,
                "pass": not failures,
                "failures": failures,
            }
        )

    matrix_passed = all(row["pass"] for row in rows)
    blockers: list[str] = []
    if not canonical["session_id"]:
        blockers.append("route receipt missing session_id")
    if not canonical["job_id"]:
        blockers.append("route receipt missing job_id")
    if not canonical["route_decision_id"]:
        blockers.append("route receipt missing route_decision_id")
    for row in rows:
        if not row["pass"]:
            blockers.append(f"{row['seam']} seam: {', '.join(row['failures'])}")

    return {
        "canonical_identity": canonical,
        "rows": rows,
        "passed": matrix_passed and not blockers,
        "blockers": blockers,
    }


def build_sp01_gate_verdict(coherence_matrix: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(coherence_matrix.get("passed", False))
    blockers = list(coherence_matrix.get("blockers") or [])
    return {
        "passed": passed,
        "reason": (
            "SP-01 same-session coherence confirmed"
            if passed
            else "SP-01 blocked: " + "; ".join(blockers)
        ),
        "blockers": blockers,
        "coherence_passed": passed,
    }


def build_sp01_packet(payload: Mapping[str, Any]) -> dict[str, Any]:
    route_receipt = build_sp01_route_receipt(payload)
    provider_receipt = build_sp01_provider_receipt(payload)
    durability_receipt = build_sp01_durability_receipt(payload)
    coherence_matrix = build_sp01_coherence_matrix(route_receipt, provider_receipt, durability_receipt)
    gate_verdict = build_sp01_gate_verdict(coherence_matrix)
    return {
        "route_receipt": route_receipt,
        "provider_receipt": provider_receipt,
        "durability_receipt": durability_receipt,
        "coherence_matrix": coherence_matrix,
        "gate_verdict": gate_verdict,
    }


def write_sp01_packet(packet: Mapping[str, Any], output_dir: str | Path) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for key, filename in SP01_EVIDENCE_FILENAMES.items():
        path = directory / filename
        path.write_text(json.dumps(packet.get(key, {}), indent=2), encoding="utf-8")
        written[key] = path
    return written