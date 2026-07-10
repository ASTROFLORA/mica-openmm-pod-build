"""
sp04_durability_truth.py — SP-04 Durability and Teardown Truth.

Implements the terminal→durability→artifact→teardown→orphan→closure chain.
All receipts share session_id / job_id / provider_job_id / terminal_state_hash.
Policy-layer only; engine architecture frozen.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SP04_EVIDENCE_FILENAMES = {
    "terminal_receipt": "terminal_receipt.json",
    "durability_receipt": "durability_receipt.json",
    "artifact_index_receipt": "artifact_index_receipt.json",
    "teardown_receipt": "teardown_receipt.json",
    "orphan_scan_receipt": "orphan_scan_receipt.json",
    "coherence_matrix": "coherence_matrix.json",
    "gate_verdict": "gate_verdict.json",
}


def _as_text(v: Any) -> str:
    return str(v or "").strip()


def _make_state_hash(session_id: str, job_id: str, terminal_state: str) -> str:
    raw = f"{session_id}:{job_id}:{terminal_state}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Receipt builders
# ---------------------------------------------------------------------------

def build_sp04_terminal_receipt(
    session_id: str,
    job_id: str,
    provider_job_id: str,
    terminal_state: str,
    event_id: str,
    *,
    success: bool = False,
    error: str = "",
) -> Dict[str, Any]:
    """
    Terminal state commit. event_id is immutable; replays must reuse same event_id.
    Callers are responsible for idempotency (same session_id → same event_id).
    """
    return {
        "schema_version": "terminal_receipt_v1",
        "session_id": session_id,
        "job_id": job_id,
        "provider_job_id": provider_job_id,
        "terminal_state": terminal_state,
        "terminal_state_hash": _make_state_hash(session_id, job_id, terminal_state),
        "event_id": event_id,
        "success": success,
        "error": error,
    }


def build_sp04_durability_receipt(
    terminal_receipt: Mapping[str, Any],
    *,
    durable: bool = True,
    storage_backend: str = "gcs",
    durability_event_id: str = "",
) -> Dict[str, Any]:
    """Durability commit must reference the terminal event."""
    return {
        "schema_version": "durability_receipt_v1",
        "session_id": _as_text(terminal_receipt.get("session_id")),
        "job_id": _as_text(terminal_receipt.get("job_id")),
        "provider_job_id": _as_text(terminal_receipt.get("provider_job_id")),
        "terminal_state_hash": _as_text(terminal_receipt.get("terminal_state_hash")),
        "terminal_event_ref": _as_text(terminal_receipt.get("event_id")),
        "durable": durable,
        "storage_backend": storage_backend,
        "durability_event_id": durability_event_id,
    }


def build_sp04_artifact_index_receipt(
    durability_receipt: Mapping[str, Any],
    artifacts: List[str],
    *,
    output_dir: str = "",
) -> Dict[str, Any]:
    """Artifact index must reference the committed terminal event via durability."""
    terminal_event_ref = _as_text(durability_receipt.get("terminal_event_ref"))
    if not terminal_event_ref:
        raise ValueError("artifact_index_receipt requires a non-empty terminal_event_ref from durability_receipt")
    return {
        "schema_version": "artifact_index_receipt_v1",
        "session_id": _as_text(durability_receipt.get("session_id")),
        "job_id": _as_text(durability_receipt.get("job_id")),
        "provider_job_id": _as_text(durability_receipt.get("provider_job_id")),
        "terminal_state_hash": _as_text(durability_receipt.get("terminal_state_hash")),
        "terminal_event_ref": terminal_event_ref,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "output_dir": output_dir,
    }


def build_sp04_teardown_receipt(
    terminal_receipt: Mapping[str, Any],
    resources_targeted: List[str],
    *,
    destroy_attempted: bool = True,
    destroy_succeeded: bool = True,
    preserved_for_recovery: bool = False,
) -> Dict[str, Any]:
    """Teardown runs only after terminal durability commit. resource_list is mandatory."""
    if not resources_targeted and not preserved_for_recovery:
        raise ValueError("teardown_receipt requires resources_targeted unless preserved_for_recovery=True")
    return {
        "schema_version": "teardown_receipt_v1",
        "session_id": _as_text(terminal_receipt.get("session_id")),
        "job_id": _as_text(terminal_receipt.get("job_id")),
        "provider_job_id": _as_text(terminal_receipt.get("provider_job_id")),
        "terminal_state_hash": _as_text(terminal_receipt.get("terminal_state_hash")),
        "resources_targeted": resources_targeted,
        "destroy_attempted": destroy_attempted,
        "destroy_succeeded": destroy_succeeded,
        "preserved_for_recovery": preserved_for_recovery,
    }


def build_sp04_orphan_scan_receipt(
    teardown_receipt: Mapping[str, Any],
    orphan_result: str,  # "none" | "detected"
    orphans_found: Optional[List[str]] = None,
    *,
    remediation_receipt_id: str = "",
) -> Dict[str, Any]:
    """
    Orphan scan is mandatory. 'detected' result marks gate failed
    until remediated with follow-up receipt.
    """
    if orphan_result not in ("none", "detected"):
        raise ValueError(f"orphan_result must be 'none' or 'detected', got {orphan_result!r}")
    return {
        "schema_version": "orphan_scan_receipt_v1",
        "session_id": _as_text(teardown_receipt.get("session_id")),
        "job_id": _as_text(teardown_receipt.get("job_id")),
        "provider_job_id": _as_text(teardown_receipt.get("provider_job_id")),
        "terminal_state_hash": _as_text(teardown_receipt.get("terminal_state_hash")),
        "orphan_result": orphan_result,
        "orphans_found": list(orphans_found or []),
        "remediation_receipt_id": remediation_receipt_id,
        "gate_passed": orphan_result == "none" or bool(remediation_receipt_id),
    }


# ---------------------------------------------------------------------------
# Coherence matrix
# ---------------------------------------------------------------------------

def build_sp04_coherence_matrix(
    terminal_receipt: Mapping[str, Any],
    durability_receipt: Mapping[str, Any],
    artifact_index_receipt: Mapping[str, Any],
    teardown_receipt: Mapping[str, Any],
    orphan_scan_receipt: Mapping[str, Any],
) -> Dict[str, Any]:
    canonical = {
        "session_id": _as_text(terminal_receipt.get("session_id")),
        "job_id": _as_text(terminal_receipt.get("job_id")),
        "provider_job_id": _as_text(terminal_receipt.get("provider_job_id")),
        "terminal_state_hash": _as_text(terminal_receipt.get("terminal_state_hash")),
    }

    rows: List[Dict[str, Any]] = []
    blockers: List[str] = []

    for seam_name, receipt in (
        ("terminal", terminal_receipt),
        ("durability", durability_receipt),
        ("artifact_index", artifact_index_receipt),
        ("teardown", teardown_receipt),
        ("orphan_scan", orphan_scan_receipt),
    ):
        checks: List[Dict[str, Any]] = []
        failures: List[str] = []
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

    # additional orphan gate check
    orphan_gate = bool(orphan_scan_receipt.get("gate_passed", False))
    if not orphan_gate:
        blockers.append("orphan_scan gate failed — orphans detected without remediation receipt")

    for row in rows:
        if not row["pass"]:
            blockers.append(f"{row['seam']} seam: {', '.join(row['failures'])}")

    return {
        "canonical_identity": canonical,
        "rows": rows,
        "passed": all(row["pass"] for row in rows) and orphan_gate and not blockers,
        "blockers": blockers,
    }


def build_sp04_gate_verdict(coherence_matrix: Mapping[str, Any]) -> Dict[str, Any]:
    passed = bool(coherence_matrix.get("passed", False))
    blockers = list(coherence_matrix.get("blockers") or [])
    return {
        "passed": passed,
        "reason": (
            "SP-04 durability and teardown truth confirmed"
            if passed
            else "SP-04 blocked: " + "; ".join(blockers)
        ),
        "blockers": blockers,
    }


def build_sp04_packet(
    session_id: str,
    job_id: str,
    provider_job_id: str,
    terminal_state: str,
    terminal_event_id: str,
    artifacts: List[str],
    resources_targeted: List[str],
    orphan_result: str,
    *,
    success: bool = False,
    error: str = "",
    durable: bool = True,
    destroy_succeeded: bool = True,
    preserved_for_recovery: bool = False,
    remediation_receipt_id: str = "",
    output_dir: str = "",
    durability_event_id: str = "",
) -> Dict[str, Any]:
    terminal = build_sp04_terminal_receipt(
        session_id, job_id, provider_job_id, terminal_state, terminal_event_id,
        success=success, error=error,
    )
    durability = build_sp04_durability_receipt(
        terminal, durable=durable, durability_event_id=durability_event_id
    )
    artifact_index = build_sp04_artifact_index_receipt(durability, artifacts, output_dir=output_dir)
    teardown = build_sp04_teardown_receipt(
        terminal, resources_targeted,
        destroy_attempted=True,
        destroy_succeeded=destroy_succeeded,
        preserved_for_recovery=preserved_for_recovery,
    )
    orphan = build_sp04_orphan_scan_receipt(
        teardown, orphan_result, remediation_receipt_id=remediation_receipt_id
    )
    coherence = build_sp04_coherence_matrix(terminal, durability, artifact_index, teardown, orphan)
    verdict = build_sp04_gate_verdict(coherence)
    return {
        "terminal_receipt": terminal,
        "durability_receipt": durability,
        "artifact_index_receipt": artifact_index,
        "teardown_receipt": teardown,
        "orphan_scan_receipt": orphan,
        "coherence_matrix": coherence,
        "gate_verdict": verdict,
    }


def write_sp04_packet(packet: Mapping[str, Any], output_dir: str | Path) -> Dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}
    for key, filename in SP04_EVIDENCE_FILENAMES.items():
        path = directory / filename
        path.write_text(json.dumps(packet.get(key, {}), indent=2), encoding="utf-8")
        written[key] = path
    return written
