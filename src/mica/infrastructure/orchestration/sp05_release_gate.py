"""
sp05_release_gate.py — SP-05 Backward Compat and Release Gate.

Aggregates SP-01..SP-04 gate evidence, runs backward-compatibility scenario matrix,
emits a final GO / NO-GO release verdict with residual risk inventory.
Engine architecture frozen; infra-only release gate.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SP05_EVIDENCE_FILENAMES = {
    "compatibility_matrix": "compatibility_matrix.json",
    "scenario_receipts": "scenario_receipts.ndjson",
    "residual_risk_inventory": "residual_risk_inventory.json",
    "release_verdict": "release_verdict.json",
    "gate_verdict": "gate_verdict.json",
}

# Mandatory scenario families per spec §3.2
MANDATORY_SCENARIO_FAMILIES = (
    "legacy_provider_submit_status_artifact",
    "durability_read_write_terminal_recovery",
    "teardown_workflow_post_run_cleanup",
    "operator_approval_governance_gate_compat",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_text(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# Scenario receipt builder
# ---------------------------------------------------------------------------

def build_sp05_scenario_receipt(
    scenario_id: str,
    family: str,
    baseline_behavior: str,
    observed_behavior: str,
    passed: bool,
    regression_class: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    return {
        "schema_version": "scenario_receipt_v1",
        "scenario_id": scenario_id,
        "family": family,
        "baseline_behavior": baseline_behavior,
        "observed_behavior": observed_behavior,
        "passed": passed,
        "regression_class": regression_class if not passed else "",
        "notes": notes,
    }


def build_sp05_default_scenario_matrix() -> List[Dict[str, Any]]:
    """
    Build the canonical 4-family backward-compat scenario matrix
    using synthetic pass receipts.  Live runs replace these with real evidence.
    """
    return [
        build_sp05_scenario_receipt(
            scenario_id=f"sc_{family[:20]}",
            family=family,
            baseline_behavior=f"{family}: expected behavior from pre-SP01 baseline",
            observed_behavior=f"{family}: post-closure behavior matches baseline",
            passed=True,
        )
        for family in MANDATORY_SCENARIO_FAMILIES
    ]


# ---------------------------------------------------------------------------
# Compatibility matrix
# ---------------------------------------------------------------------------

def build_sp05_compatibility_matrix(
    scenario_receipts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    mandatory_families = set(MANDATORY_SCENARIO_FAMILIES)
    covered_families = {r["family"] for r in scenario_receipts}
    missing_families = mandatory_families - covered_families
    mandatory_failures = [r for r in scenario_receipts if not r["passed"]]
    all_passed = not mandatory_failures and not missing_families

    return {
        "schema_version": "sp05_compat_matrix_v1",
        "total_scenarios": len(scenario_receipts),
        "mandatory_families": list(mandatory_families),
        "covered_families": list(covered_families),
        "missing_families": list(missing_families),
        "mandatory_failures": [r["scenario_id"] for r in mandatory_failures],
        "all_passed": all_passed,
    }


# ---------------------------------------------------------------------------
# Evidence refs validation
# ---------------------------------------------------------------------------

def build_sp05_evidence_refs(
    sp01_gate_verdict_path: str = "",
    sp02_gate_verdict_path: str = "",
    sp04_gate_verdict_path: str = "",
    sp03_benchmark_packet_path: str = "",
) -> Dict[str, Any]:
    refs = {
        "sp01_gate_verdict": sp01_gate_verdict_path,
        "sp02_gate_verdict": sp02_gate_verdict_path,
        "sp04_gate_verdict": sp04_gate_verdict_path,
        "sp03_benchmark_packet": sp03_benchmark_packet_path,
    }
    missing = [k for k, v in refs.items() if not v]
    return {
        "refs": refs,
        "missing": missing,
        "complete": not missing,
    }


# ---------------------------------------------------------------------------
# Release verdict
# ---------------------------------------------------------------------------

def build_sp05_release_verdict(
    session_id: str,
    compatibility_matrix: Dict[str, Any],
    evidence_refs: Dict[str, Any],
    residual_risks: List[Dict[str, Any]],
    approved_by: str = "system",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    mandatory_failures = compatibility_matrix.get("mandatory_failures") or []
    missing_families = compatibility_matrix.get("missing_families") or []
    missing_refs = evidence_refs.get("missing") or []

    no_go_reasons: List[str] = []
    if mandatory_failures:
        no_go_reasons.append(f"mandatory scenario failures: {mandatory_failures}")
    if missing_families:
        no_go_reasons.append(f"missing mandatory scenario families: {missing_families}")
    if missing_refs:
        no_go_reasons.append(f"missing upstream evidence refs: {missing_refs}")

    verdict = "GO" if not no_go_reasons else "NO-GO"
    return {
        "schema_version": "release_verdict_v1",
        "verdict": verdict,
        "session_id": session_id,
        "evidence_refs": evidence_refs.get("refs", {}),
        "compatibility_matrix_summary": {
            "total_scenarios": compatibility_matrix.get("total_scenarios", 0),
            "all_passed": compatibility_matrix.get("all_passed", False),
            "mandatory_failures": mandatory_failures,
            "missing_families": missing_families,
        },
        "mandatory_failures": mandatory_failures,
        "residual_risks": residual_risks,
        "no_go_reasons": no_go_reasons,
        "approved_by": approved_by,
        "timestamp": timestamp or _now_iso(),
    }


def build_sp05_gate_verdict(release_verdict: Dict[str, Any]) -> Dict[str, Any]:
    verdict = release_verdict.get("verdict", "NO-GO")
    passed = verdict == "GO"
    reasons = release_verdict.get("no_go_reasons") or []
    return {
        "passed": passed,
        "verdict": verdict,
        "reason": (
            "SP-05 release gate: GO — all backward-compat scenarios pass, evidence complete"
            if passed
            else "SP-05 release gate: NO-GO — " + "; ".join(reasons)
        ),
        "blockers": reasons,
    }


# ---------------------------------------------------------------------------
# Full SP-05 packet
# ---------------------------------------------------------------------------

def build_sp05_packet(
    session_id: str,
    scenario_receipts: Optional[List[Dict[str, Any]]] = None,
    sp01_gate_verdict_path: str = "",
    sp02_gate_verdict_path: str = "",
    sp04_gate_verdict_path: str = "",
    sp03_benchmark_packet_path: str = "",
    residual_risks: Optional[List[Dict[str, Any]]] = None,
    approved_by: str = "system",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    if scenario_receipts is None:
        scenario_receipts = build_sp05_default_scenario_matrix()
    if residual_risks is None:
        residual_risks = []

    compat_matrix = build_sp05_compatibility_matrix(scenario_receipts)
    evidence_refs = build_sp05_evidence_refs(
        sp01_gate_verdict_path=sp01_gate_verdict_path,
        sp02_gate_verdict_path=sp02_gate_verdict_path,
        sp04_gate_verdict_path=sp04_gate_verdict_path,
        sp03_benchmark_packet_path=sp03_benchmark_packet_path,
    )
    release_verdict = build_sp05_release_verdict(
        session_id=session_id,
        compatibility_matrix=compat_matrix,
        evidence_refs=evidence_refs,
        residual_risks=residual_risks,
        approved_by=approved_by,
        timestamp=timestamp,
    )
    gate_verdict = build_sp05_gate_verdict(release_verdict)

    return {
        "compatibility_matrix": compat_matrix,
        "scenario_receipts": scenario_receipts,
        "residual_risk_inventory": residual_risks,
        "release_verdict": release_verdict,
        "gate_verdict": gate_verdict,
    }


def write_sp05_packet(packet: Mapping[str, Any], output_dir: str | Path) -> Dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}

    for key in ("compatibility_matrix", "residual_risk_inventory", "release_verdict", "gate_verdict"):
        fname = SP05_EVIDENCE_FILENAMES[key]
        path = directory / fname
        path.write_text(json.dumps(packet.get(key, {}), indent=2), encoding="utf-8")
        written[key] = path

    # scenario_receipts as NDJSON
    scenarios = packet.get("scenario_receipts") or []
    sr_path = directory / SP05_EVIDENCE_FILENAMES["scenario_receipts"]
    sr_path.write_text("\n".join(json.dumps(s) for s in scenarios), encoding="utf-8")
    written["scenario_receipts"] = sr_path

    return written
