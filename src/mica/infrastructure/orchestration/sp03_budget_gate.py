"""
sp03_budget_gate.py — SP-03 Cost Gate and Retail Model.

Orchestrates the launch envelope: quote -> approval -> (override?) -> audit -> verdict.
Returns one immutable governance decision packet for the submit path.
Policy-layer only; does not alter scientific engine internals.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from mica.sim.budget_governance_contracts import (
    ApprovalDecision,
    AuditEventType,
    BudgetApprovalV1,
    BudgetAuditLedger,
    BudgetAuditLedgerEntryV1,
    BudgetOverrideV1,
    BudgetQuoteV1,
)


SP03_EVIDENCE_FILENAMES = {
    "quote": "quote.json",
    "approval": "approval.json",
    "override": "override.json",
    "audit_ledger": "audit_ledger.ndjson",
    "coherence_matrix": "coherence_matrix.json",
    "gate_verdict": "gate_verdict.json",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_text(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# Receipt coherence
# ---------------------------------------------------------------------------

def build_sp03_coherence_matrix(
    session_id: str,
    quote: BudgetQuoteV1,
    approval: BudgetApprovalV1,
    override: Optional[BudgetOverrideV1],
) -> Dict[str, Any]:
    """All objects must share the same session_id and quote_id linkage."""
    rows: List[Dict[str, Any]] = []
    blockers: List[str] = []

    # session_id checks
    for obj_name, obj_session in (
        ("quote", quote.session_id),
        ("approval", approval.session_id),
        *([("override", override.session_id)] if override else []),
    ):
        ok = bool(session_id) and obj_session == session_id
        if not ok:
            blockers.append(f"{obj_name}.session_id mismatch or missing")
        rows.append({"object": obj_name, "field": "session_id", "expected": session_id, "actual": obj_session, "pass": ok})

    # quote_id linkage
    approval_quote_ok = approval.quote_id == quote.quote_id
    if not approval_quote_ok:
        blockers.append("approval.quote_id does not match quote.quote_id")
    rows.append({"object": "approval", "field": "quote_id", "expected": quote.quote_id, "actual": approval.quote_id, "pass": approval_quote_ok})

    if override:
        ov_ok = override.quote_id == quote.quote_id and override.approval_id == approval.approval_id
        if not ov_ok:
            blockers.append("override.quote_id or override.approval_id linkage broken")
        rows.append({"object": "override", "field": "quote_id+approval_id", "expected": f"{quote.quote_id}+{approval.approval_id}", "actual": f"{override.quote_id}+{override.approval_id}", "pass": ov_ok})

    return {
        "session_id": session_id,
        "rows": rows,
        "passed": not blockers,
        "blockers": blockers,
    }


# ---------------------------------------------------------------------------
# Gate verdict
# ---------------------------------------------------------------------------

def build_sp03_gate_verdict(
    approval: BudgetApprovalV1,
    override: Optional[BudgetOverrideV1],
    coherence_matrix: Dict[str, Any],
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Launch is ALLOWED only when:
    1. Coherence passes.
    2. Approval does not block, OR a valid non-expired override exists.
    """
    blockers: List[str] = list(coherence_matrix.get("blockers") or [])

    if not coherence_matrix.get("passed"):
        pass  # blockers already captured above

    blocked_by_approval = approval.blocks_launch()

    # If approval blocks, check override
    override_active = False
    if blocked_by_approval:
        if override is None:
            blockers.append(f"approval decision={approval.decision.value} requires override but none provided")
        elif override.is_expired(now_iso):
            blockers.append(f"approval decision={approval.decision.value} but override expired at {override.expires_at}")
        else:
            override_active = True  # valid non-expired override unblocks

    launch_allowed = not blockers and (not blocked_by_approval or override_active)

    return {
        "launch_allowed": launch_allowed,
        "decision": approval.decision.value,
        "override_active": override_active,
        "coherence_passed": bool(coherence_matrix.get("passed")),
        "reason": (
            "SP-03 budget governance: launch allowed"
            if launch_allowed
            else "SP-03 budget governance: launch blocked — " + "; ".join(blockers)
        ),
        "blockers": blockers,
    }


# ---------------------------------------------------------------------------
# Full governance packet
# ---------------------------------------------------------------------------

def build_sp03_packet(
    quote: BudgetQuoteV1,
    approval: BudgetApprovalV1,
    override: Optional[BudgetOverrideV1],
    ledger: BudgetAuditLedger,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    if now_iso is None:
        now_iso = _now_iso()

    coherence_matrix = build_sp03_coherence_matrix(
        session_id=quote.session_id,
        quote=quote,
        approval=approval,
        override=override,
    )
    gate_verdict = build_sp03_gate_verdict(
        approval=approval,
        override=override,
        coherence_matrix=coherence_matrix,
        now_iso=now_iso,
    )

    return {
        "schema_version": "sp03_budget_gate_v1",
        "session_id": quote.session_id,
        "job_id": quote.job_id,
        "quote": quote.to_dict(),
        "approval": approval.to_dict(),
        "override": override.to_dict() if override else None,
        "audit_ledger_entries": [e.to_dict() for e in ledger.entries()],
        "coherence_matrix": coherence_matrix,
        "gate_verdict": gate_verdict,
    }


def write_sp03_packet(packet: Mapping[str, Any], output_dir: str | Path) -> Dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}

    for key in ("quote", "approval", "coherence_matrix", "gate_verdict"):
        fname = SP03_EVIDENCE_FILENAMES[key]
        path = directory / fname
        path.write_text(json.dumps(packet.get(key) or {}, indent=2), encoding="utf-8")
        written[key] = path

    # override (may be None)
    ov_path = directory / SP03_EVIDENCE_FILENAMES["override"]
    ov_path.write_text(json.dumps(packet.get("override") or {}, indent=2), encoding="utf-8")
    written["override"] = ov_path

    # audit ledger as NDJSON
    ledger_path = directory / SP03_EVIDENCE_FILENAMES["audit_ledger"]
    entries = packet.get("audit_ledger_entries") or []
    ledger_path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    written["audit_ledger"] = ledger_path

    return written


# ---------------------------------------------------------------------------
# Passthrough gate stamp — emitted on every submit receipt when no full
# quote/approval cycle has been run. Makes the SP-03 surface observable on
# all sim:job/submit receipts so future enforcement can upgrade without
# changing the receipt schema.
# ---------------------------------------------------------------------------

def build_sp03_gate_stamp(
    session_id: str,
    job_id: str,
    *,
    reason_code: str = "cost_gate_not_evaluated",
    launch_allowed: bool = True,
    decision: str = "ALLOW",
    override_active: bool = False,
) -> Dict[str, Any]:
    """Lightweight SP-03 gate stamp for submit receipts that have no quote/approval.

    Emitted as ``receipt["sp03_gate_stamp"]`` in ``sim_adapter._hit_job_submit``
    when no full ``BudgetQuoteV1 -> BudgetApprovalV1`` cycle exists.  Keeps
    the SP-03 surface visible and schema-stable; callers can upgrade to a
    full ``build_sp03_packet()`` call without altering downstream receipt
    consumers.
    """
    return {
        "schema_version": "sp03_gate_stamp_v1",
        "session_id": session_id,
        "job_id": job_id,
        "launch_allowed": launch_allowed,
        "decision": decision,
        "override_active": override_active,
        "reason_code": reason_code,
        "blockers": [] if launch_allowed else [reason_code],
    }

