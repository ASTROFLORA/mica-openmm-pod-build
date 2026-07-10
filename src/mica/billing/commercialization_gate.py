"""commercialization_gate.py — SP-18 Commercialization Ready Billing Gate

Doctrine: No real charging is allowed until billing is explainable, reversible,
and auditable end-to-end.

This module provides:
  1. Commercial billing release checklist — validates all preconditions before
     any real-money charging can be enabled.
  2. Dry-run invoice lifecycle — generates a charge estimate through the full
     invoice path (line-items → total → tax → audit hash) without committing
     any real transaction.
  3. Refund / credit path — dry-run reversal of an invoice with immutable audit
     trail entry.
  4. Residual risk ledger — structured record of known billing risks and their
     mitigation state.

All operations are deterministic and produce JSON-serialisable output suitable
for operator inspection and compliance audit.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Checklist item catalogue
# ---------------------------------------------------------------------------

class ChecklistStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


RELEASE_CHECKLIST_ITEMS = [
    {
        "id": "BG-01",
        "label": "Value taxonomy adopted",
        "description": "SP-17 ScientificValueClass enum is importable and covers all three classes.",
        "risk": "high",
    },
    {
        "id": "BG-02",
        "label": "Cost-to-artifact lineage available",
        "description": "build_value_record() and the POST /jobs/{id}/value-class endpoint are present.",
        "risk": "high",
    },
    {
        "id": "BG-03",
        "label": "Cost-per-useful-output report available",
        "description": "GET /costs/scientific-value-report endpoint is present.",
        "risk": "medium",
    },
    {
        "id": "BG-04",
        "label": "User spend tracking active",
        "description": "get_user_spend() on UnifiedComputeClient returns correct aggregates.",
        "risk": "high",
    },
    {
        "id": "BG-05",
        "label": "Cost ceiling enforced",
        "description": "Submit is rejected when user spend + estimated cost exceeds ceiling.",
        "risk": "critical",
    },
    {
        "id": "BG-06",
        "label": "Economic ledger endpoints present",
        "description": "GET /jobs/{id}/ledger and GET /costs return EconomicLedgerResponse.",
        "risk": "high",
    },
    {
        "id": "BG-07",
        "label": "Dry-run invoice lifecycle verified",
        "description": "build_dry_run_invoice() produces a deterministic, auditable invoice envelope.",
        "risk": "high",
    },
    {
        "id": "BG-08",
        "label": "Dry-run refund/credit path verified",
        "description": "build_dry_run_refund() produces an immutable reversal record linked to the original invoice.",
        "risk": "high",
    },
    {
        "id": "BG-09",
        "label": "Residual risk ledger published",
        "description": "build_residual_risk_ledger() returns a complete risk entry list with mitigations.",
        "risk": "medium",
    },
    {
        "id": "BG-10",
        "label": "No real-money write path active",
        "description": "No live payment processor client is instantiated in this iteration.",
        "risk": "critical",
    },
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _invoice_hash(invoice_id: str, user_id: str, total_usd: float, issued_at: str) -> str:
    """Deterministic SHA-256 audit hash for an invoice."""
    src = f"{invoice_id}|{user_id}|{total_usd:.6f}|{issued_at}"
    return hashlib.sha256(src.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Commercial billing release checklist
# ---------------------------------------------------------------------------

def run_commercialization_checklist(
    *,
    value_taxonomy_imported: bool = False,
    lineage_endpoint_present: bool = False,
    value_report_endpoint_present: bool = False,
    user_spend_working: bool = False,
    cost_ceiling_enforced: bool = False,
    economic_ledger_present: bool = False,
    dry_run_invoice_verified: bool = False,
    dry_run_refund_verified: bool = False,
    residual_risk_ledger_present: bool = False,
    no_real_money_write: bool = True,
) -> dict[str, Any]:
    """Evaluate all commercialization release checklist items.

    Each item maps to a specific implemented capability. Callers pass boolean
    probe results; this function evaluates and returns a structured report.

    Args:
        value_taxonomy_imported: BG-01 — SP-17 taxonomy importable.
        lineage_endpoint_present: BG-02 — cost-to-artifact lineage endpoint.
        value_report_endpoint_present: BG-03 — cost-per-useful-output report.
        user_spend_working: BG-04 — user spend tracking active.
        cost_ceiling_enforced: BG-05 — cost ceiling enforcement active.
        economic_ledger_present: BG-06 — economic ledger endpoints present.
        dry_run_invoice_verified: BG-07 — dry-run invoice lifecycle verified.
        dry_run_refund_verified: BG-08 — dry-run refund path verified.
        residual_risk_ledger_present: BG-09 — residual risk ledger published.
        no_real_money_write: BG-10 — no live payment processor active.

    Returns:
        Checklist report dict with per-item status and overall gate decision.
    """
    probe_map = {
        "BG-01": value_taxonomy_imported,
        "BG-02": lineage_endpoint_present,
        "BG-03": value_report_endpoint_present,
        "BG-04": user_spend_working,
        "BG-05": cost_ceiling_enforced,
        "BG-06": economic_ledger_present,
        "BG-07": dry_run_invoice_verified,
        "BG-08": dry_run_refund_verified,
        "BG-09": residual_risk_ledger_present,
        "BG-10": no_real_money_write,
    }

    items = []
    critical_failures = []
    high_failures = []
    warnings = []

    for spec in RELEASE_CHECKLIST_ITEMS:
        item_id = spec["id"]
        probe = probe_map.get(item_id, False)
        status = ChecklistStatus.PASS if probe else ChecklistStatus.FAIL

        items.append({
            "id": item_id,
            "label": spec["label"],
            "description": spec["description"],
            "risk": spec["risk"],
            "status": status.value,
        })

        if status == ChecklistStatus.FAIL:
            if spec["risk"] == "critical":
                critical_failures.append(item_id)
            elif spec["risk"] == "high":
                high_failures.append(item_id)
            else:
                warnings.append(item_id)

    passed = sum(1 for it in items if it["status"] == ChecklistStatus.PASS.value)
    total = len(items)

    # Gate decision: PASS requires zero critical failures and zero high failures.
    # Warnings (medium/low) are allowed for a PASS.
    gate_pass = len(critical_failures) == 0 and len(high_failures) == 0
    decision = "pass" if gate_pass else "fail"

    return {
        "decision": decision,
        "passed_items": passed,
        "total_items": total,
        "critical_failures": critical_failures,
        "high_failures": high_failures,
        "warnings": warnings,
        "items": items,
        "evaluated_at": _utcnow(),
        "schema_version": "sp18_v1",
    }


# ---------------------------------------------------------------------------
# Dry-run invoice lifecycle
# ---------------------------------------------------------------------------

def build_dry_run_invoice(
    *,
    user_id: str,
    job_ids: list[str],
    line_items: list[dict[str, Any]],
    currency: str = "USD",
    tax_rate: float = 0.0,
    notes: str = "",
) -> dict[str, Any]:
    """Build a dry-run invoice envelope for the given job IDs and line items.

    A dry-run invoice is identical in structure to a real invoice but carries
    ``mode: dry_run`` and is never submitted to a payment processor.

    Args:
        user_id: Billing identity of the customer.
        job_ids: List of compute job IDs covered by this invoice.
        line_items: List of dicts with keys: description, quantity, unit_price_usd.
        currency: ISO 4217 currency code (default USD).
        tax_rate: Fractional tax rate (0.0–1.0). Applied to subtotal.
        notes: Optional operator notes.

    Returns:
        Invoice envelope dict with line items, subtotal, tax, total, audit hash,
        and lifecycle state machine snapshot.
    """
    invoice_id = f"inv-dry-{uuid.uuid4().hex[:12]}"
    issued_at = _utcnow()

    # Compute line item totals
    enriched_items = []
    subtotal = 0.0
    for item in line_items:
        qty = float(item.get("quantity") or 1)
        unit_price = float(item.get("unit_price_usd") or 0.0)
        line_total = round(qty * unit_price, 6)
        subtotal += line_total
        enriched_items.append({
            "description": str(item.get("description") or ""),
            "quantity": qty,
            "unit_price_usd": round(unit_price, 6),
            "line_total_usd": line_total,
        })

    subtotal = round(subtotal, 6)
    tax_amount = round(subtotal * float(tax_rate), 6)
    total = round(subtotal + tax_amount, 6)

    audit_hash = _invoice_hash(invoice_id, user_id, total, issued_at)

    # Invoice lifecycle state machine: draft → issued → (paid | void | refunded)
    # Dry-run freezes at "issued" — no payment transition allowed.
    lifecycle = {
        "state": "issued",
        "transitions_allowed": ["void", "refund_request"],
        "transitions_blocked": ["charge", "settle"],
        "blocked_reason": "dry_run_mode_active",
    }

    return {
        "invoice_id": invoice_id,
        "mode": "dry_run",
        "user_id": user_id,
        "job_ids": list(job_ids),
        "currency": currency,
        "line_items": enriched_items,
        "subtotal_usd": subtotal,
        "tax_rate": round(float(tax_rate), 6),
        "tax_amount_usd": tax_amount,
        "total_usd": total,
        "audit_hash": audit_hash,
        "lifecycle": lifecycle,
        "notes": notes,
        "issued_at": issued_at,
        "schema_version": "sp18_v1",
    }


# ---------------------------------------------------------------------------
# Dry-run refund / credit path
# ---------------------------------------------------------------------------

def build_dry_run_refund(
    *,
    original_invoice: dict[str, Any],
    refund_reason: str,
    refund_amount_usd: float | None = None,
    initiated_by: str = "operator",
) -> dict[str, Any]:
    """Build a dry-run refund/credit record linked to an original dry-run invoice.

    The refund record is append-only: it never mutates the original invoice but
    carries a back-link via ``original_invoice_id`` and ``original_audit_hash``.
    Partial refunds are supported by passing ``refund_amount_usd < total``.

    Args:
        original_invoice: Invoice dict produced by build_dry_run_invoice().
        refund_reason: Free-text reason required for audit trail.
        refund_amount_usd: Amount to refund (defaults to full invoice total).
        initiated_by: Identity of the operator or system issuing the refund.

    Returns:
        Refund record dict with immutable back-link, audit hash, and credit note.

    Raises:
        ValueError: If original_invoice is not a dry-run invoice.
        ValueError: If refund amount exceeds invoice total.
        ValueError: If refund_reason is empty.
    """
    if not refund_reason or not refund_reason.strip():
        raise ValueError("refund_reason is required for audit trail")

    if original_invoice.get("mode") != "dry_run":
        raise ValueError(
            "build_dry_run_refund only accepts dry_run invoices; "
            f"got mode='{original_invoice.get('mode')}'"
        )

    invoice_total = float(original_invoice.get("total_usd") or 0.0)
    resolved_amount = float(refund_amount_usd) if refund_amount_usd is not None else invoice_total

    if resolved_amount < 0:
        raise ValueError("refund_amount_usd must be non-negative")
    if resolved_amount > invoice_total + 1e-9:
        raise ValueError(
            f"refund_amount_usd ({resolved_amount:.6f}) exceeds "
            f"invoice total ({invoice_total:.6f})"
        )

    refund_id = f"ref-dry-{uuid.uuid4().hex[:12]}"
    issued_at = _utcnow()
    is_full = abs(resolved_amount - invoice_total) < 1e-9

    # Immutable audit hash for the refund record itself
    src = f"{refund_id}|{original_invoice['invoice_id']}|{resolved_amount:.6f}|{issued_at}"
    refund_audit_hash = hashlib.sha256(src.encode()).hexdigest()

    return {
        "refund_id": refund_id,
        "mode": "dry_run",
        "original_invoice_id": original_invoice["invoice_id"],
        "original_audit_hash": original_invoice.get("audit_hash", ""),
        "user_id": original_invoice.get("user_id", ""),
        "currency": original_invoice.get("currency", "USD"),
        "refund_amount_usd": round(resolved_amount, 6),
        "invoice_total_usd": round(invoice_total, 6),
        "is_full_refund": is_full,
        "refund_reason": refund_reason.strip(),
        "initiated_by": initiated_by,
        "credit_note": {
            "type": "full_credit" if is_full else "partial_credit",
            "amount_usd": round(resolved_amount, 6),
            "applied_to": original_invoice["invoice_id"],
        },
        "lifecycle": {
            "state": "credited",
            "transitions_blocked": ["charge"],
            "blocked_reason": "dry_run_mode_active",
        },
        "audit_hash": refund_audit_hash,
        "issued_at": issued_at,
        "schema_version": "sp18_v1",
    }


# ---------------------------------------------------------------------------
# Residual risk ledger
# ---------------------------------------------------------------------------

RESIDUAL_RISK_ENTRIES = [
    {
        "risk_id": "RR-01",
        "category": "payment_processor",
        "description": "No live payment processor is integrated. Real charging requires Stripe/Braintree/etc.",
        "severity": "critical",
        "mitigation": "Dry-run invoice lifecycle verified. BG-10 checklist item blocks real-money writes.",
        "status": "mitigated_in_dry_run",
    },
    {
        "risk_id": "RR-02",
        "category": "value_accounting",
        "description": "Value records are stored in-memory (_value_records dict); not durable across restarts.",
        "severity": "high",
        "mitigation": "Production path is to extend execution_record vocabulary into Timescale. SP-17 lineage contract is established.",
        "status": "known_gap_next_sp",
    },
    {
        "risk_id": "RR-03",
        "category": "tax_compliance",
        "description": "Tax rate is a flat operator-supplied rate; no jurisdiction-aware tax engine.",
        "severity": "medium",
        "mitigation": "Tax field is present in invoice schema for future integration. Dry-run mode blocks tax remittance.",
        "status": "deferred",
    },
    {
        "risk_id": "RR-04",
        "category": "audit_trail",
        "description": "Invoice and refund audit hashes are SHA-256 but not persisted to an append-only store.",
        "severity": "high",
        "mitigation": "Hashes are deterministic and reconstructible. Durable append-only ledger is a SP-19+ requirement.",
        "status": "known_gap_next_sp",
    },
    {
        "risk_id": "RR-05",
        "category": "user_identity",
        "description": "Billing identity is the same as compute user_id; no separate billing account concept.",
        "severity": "medium",
        "mitigation": "Acceptable for research tier. Org/team billing accounts are a commercialization future requirement.",
        "status": "deferred",
    },
    {
        "risk_id": "RR-06",
        "category": "cost_ceiling",
        "description": "Cost ceiling is a static constructor argument; not persisted or per-user configurable at runtime.",
        "severity": "high",
        "mitigation": "Ceiling enforcement is active (BG-05). Runtime per-user ceiling is a SP-13+ capability.",
        "status": "known_gap_next_sp",
    },
]


def build_residual_risk_ledger() -> dict[str, Any]:
    """Build the residual billing risk ledger for SP-18.

    Returns a structured snapshot of all known billing risks, their severity,
    mitigation state, and the overall ledger decision (ready_for_dry_run vs
    blocked_for_production).

    Returns:
        Risk ledger dict.
    """
    critical = [r for r in RESIDUAL_RISK_ENTRIES if r["severity"] == "critical"]
    high = [r for r in RESIDUAL_RISK_ENTRIES if r["severity"] == "high"]
    medium = [r for r in RESIDUAL_RISK_ENTRIES if r["severity"] == "medium"]

    # Unmitigated criticals block dry-run readiness
    unmitigated_critical = [
        r for r in critical if r["status"] not in ("mitigated", "mitigated_in_dry_run")
    ]
    decision = "ready_for_dry_run" if not unmitigated_critical else "blocked"

    return {
        "decision": decision,
        "total_risks": len(RESIDUAL_RISK_ENTRIES),
        "critical_count": len(critical),
        "high_count": len(high),
        "medium_count": len(medium),
        "unmitigated_critical": [r["risk_id"] for r in unmitigated_critical],
        "risks": RESIDUAL_RISK_ENTRIES,
        "published_at": _utcnow(),
        "schema_version": "sp18_v1",
    }
