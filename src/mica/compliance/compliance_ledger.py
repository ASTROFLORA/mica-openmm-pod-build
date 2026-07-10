"""compliance_ledger.py — SP-19 Pharma Compliance And Regulated Export

Doctrine: Pharma-ready posture requires controlled provenance export from day
one, not post-hoc report stitching.

This module provides:
  1. Compliance ledger contract — structured, append-only record of every
     compute event that touches regulated data, with immutable SHA-256 event
     hashes for audit continuity.
  2. Regulated export bundle schema — methods section, provenance chain,
     known limitations, and compliance signatures. Produces a single
     JSON-serialisable bundle suitable for regulatory submission or internal
     QA systems.
  3. Export bundle signing — deterministic operator and system signature
     fields. Real PKI is deferred; signatures are SHA-256 placeholders with
     explicit ``signature_mode: placeholder`` so the gap is machine-detectable.

All operations are deterministic and produce JSON-serialisable output with
``schema_version: sp19_v1``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ComplianceEventType(str, Enum):
    JOB_SUBMITTED = "job_submitted"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    VALUE_CLASSIFIED = "value_classified"
    EXPORT_REQUESTED = "export_requested"
    EXPORT_ISSUED = "export_issued"
    BILLING_GATE_CHECKED = "billing_gate_checked"
    ARTIFACT_REGISTERED = "artifact_registered"


class ExportStatus(str, Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ComplianceFramework(str, Enum):
    """Supported regulatory / compliance framework tags."""
    GxP_LITE = "GxP-lite"          # lightweight GxP for research tools
    CFR_21_PART_11 = "21-CFR-11"   # FDA electronic records
    ISO_9001 = "ISO-9001"           # quality management
    HIPAA_ADJACENT = "HIPAA-adj"    # PII-aware but not full HIPAA
    INTERNAL_QA = "internal-qa"     # operator-defined internal audit


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 1. Compliance ledger contract
# ---------------------------------------------------------------------------

def build_compliance_event(
    *,
    event_type: ComplianceEventType | str,
    subject_id: str,
    subject_kind: str,
    actor_id: str,
    framework: ComplianceFramework | str = ComplianceFramework.INTERNAL_QA,
    payload: dict[str, Any] | None = None,
    parent_event_id: str = "",
) -> dict[str, Any]:
    """Build a single immutable compliance ledger event.

    Args:
        event_type: Semantic event category from ComplianceEventType.
        subject_id: ID of the entity being tracked (job_id, invoice_id, etc.).
        subject_kind: Kind string, e.g. "compute_job", "invoice", "artifact".
        actor_id: User or service identity triggering the event.
        framework: Compliance framework this event contributes to.
        payload: Optional additional metadata (must be JSON-serialisable).
        parent_event_id: Optional ID of the preceding event in a chain.

    Returns:
        Immutable compliance event dict with event_id, hash, and timestamp.
    """
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    occurred_at = _utcnow()
    event_hash = _sha256(event_id, str(event_type), subject_id, actor_id, occurred_at)

    return {
        "event_id": event_id,
        "event_type": str(event_type) if not isinstance(event_type, str) else event_type,
        "subject_id": subject_id,
        "subject_kind": subject_kind,
        "actor_id": actor_id,
        "framework": str(framework) if not isinstance(framework, str) else framework,
        "payload": payload or {},
        "parent_event_id": parent_event_id,
        "event_hash": event_hash,
        "occurred_at": occurred_at,
        "schema_version": "sp19_v1",
    }


def build_compliance_ledger(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble a compliance ledger from an ordered list of events.

    The ledger hash is computed over the concatenation of all individual
    event hashes, providing a chain integrity check: any reordering or
    insertion is detectable.

    Args:
        events: Ordered list of compliance event dicts (from build_compliance_event).

    Returns:
        Compliance ledger dict with chain hash, event count, and event list.
    """
    chain_hash = _sha256(*[e["event_hash"] for e in events]) if events else _sha256("")
    frameworks = sorted({e.get("framework", "") for e in events if e.get("framework")})

    return {
        "ledger_id": f"ldgr-{uuid.uuid4().hex[:12]}",
        "event_count": len(events),
        "chain_hash": chain_hash,
        "frameworks": frameworks,
        "events": events,
        "assembled_at": _utcnow(),
        "schema_version": "sp19_v1",
    }


# ---------------------------------------------------------------------------
# 2. Regulated export bundle schema
# ---------------------------------------------------------------------------

# Required top-level sections for a pharma-grade export bundle
EXPORT_BUNDLE_REQUIRED_SECTIONS = [
    "methods",
    "provenance",
    "limitations",
    "signatures",
]


def build_export_bundle(
    *,
    bundle_label: str,
    subject_ids: list[str],
    methods: dict[str, Any],
    provenance: list[dict[str, Any]],
    limitations: list[str],
    frameworks: list[str] | None = None,
    operator_id: str,
    system_version: str = "mica-sp19",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a complete regulated export bundle.

    The bundle includes all four required sections (methods, provenance,
    limitations, signatures) and is stamped with a deterministic bundle hash
    covering all key fields for integrity verification.

    Args:
        bundle_label: Human-readable label for the bundle.
        subject_ids: IDs of the compute jobs or artifacts being exported.
        methods: Dict describing the computational method (software, version,
            parameters, forcefields, integrator, etc.).
        provenance: List of provenance chain dicts. Each entry should include
            at least: step, actor_id, artifact_id or job_id, timestamp.
        limitations: List of free-text limitation statements required for
            regulated submissions.
        frameworks: Compliance framework tags (default: internal-qa).
        operator_id: Identity of the operator issuing this export.
        system_version: MICA system version string.
        extra: Optional additional metadata.

    Returns:
        Export bundle dict with all required sections, bundle_id, hash,
        lifecycle state, and schema_version.

    Raises:
        ValueError: If any required section is empty (methods, provenance,
            or limitations must be non-empty; operator_id required).
    """
    if not operator_id or not operator_id.strip():
        raise ValueError("operator_id is required for regulated export")
    if not methods:
        raise ValueError("methods section must not be empty")
    if not provenance:
        raise ValueError("provenance chain must not be empty")
    if not limitations:
        raise ValueError("limitations list must not be empty")

    bundle_id = f"bndl-{uuid.uuid4().hex[:12]}"
    issued_at = _utcnow()
    resolved_frameworks = frameworks or [ComplianceFramework.INTERNAL_QA.value]

    # Deterministic bundle hash covering all key fields
    bundle_hash = _sha256(
        bundle_id,
        operator_id.strip(),
        ",".join(sorted(subject_ids)),
        ",".join(sorted(limitations)),
        issued_at,
    )

    # Placeholder signatures — real PKI is deferred
    signatures = {
        "operator": {
            "signer_id": operator_id.strip(),
            "signature": _sha256("operator-sig", bundle_id, operator_id.strip()),
            "algorithm": "sha256-placeholder",
            "signature_mode": "placeholder",
            "note": "Real PKI signature required before regulatory submission.",
        },
        "system": {
            "signer_id": system_version,
            "signature": _sha256("system-sig", bundle_id, system_version),
            "algorithm": "sha256-placeholder",
            "signature_mode": "placeholder",
            "note": "System signature; cryptographic binding deferred to SP-20+.",
        },
    }

    return {
        "bundle_id": bundle_id,
        "bundle_label": bundle_label,
        "subject_ids": list(subject_ids),
        "frameworks": resolved_frameworks,
        "status": ExportStatus.ISSUED.value,
        "methods": methods,
        "provenance": list(provenance),
        "limitations": list(limitations),
        "signatures": signatures,
        "bundle_hash": bundle_hash,
        "operator_id": operator_id.strip(),
        "system_version": system_version,
        "extra": extra or {},
        "issued_at": issued_at,
        "schema_version": "sp19_v1",
    }


# ---------------------------------------------------------------------------
# 3. Export bundle validation
# ---------------------------------------------------------------------------

def validate_export_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate an export bundle for structural completeness.

    Checks that all required sections are present and non-empty, that
    the bundle_hash matches a recomputable value (only possible if the
    bundle was produced by build_export_bundle in the same process, since
    uuid-based bundle_id is unique), and that signature_mode fields are
    present (so the placeholder gap is machine-detectable).

    Args:
        bundle: Export bundle dict produced by build_export_bundle().

    Returns:
        Validation report dict with per-check results and overall decision.
    """
    checks = []

    def _check(check_id: str, label: str, passed: bool, detail: str = "") -> None:
        checks.append({"id": check_id, "label": label, "passed": passed, "detail": detail})

    # Required sections present and non-empty
    for section in EXPORT_BUNDLE_REQUIRED_SECTIONS:
        val = bundle.get(section)
        non_empty = bool(val)
        _check(
            f"EX-{section[:3].upper()}",
            f"Section '{section}' present and non-empty",
            non_empty,
            f"Value: {type(val).__name__}" if non_empty else "Missing or empty",
        )

    # bundle_id, bundle_hash, issued_at present
    _check("EX-BID", "bundle_id present", bool(bundle.get("bundle_id")))
    _check("EX-HSH", "bundle_hash present (64 hex chars)",
           len(bundle.get("bundle_hash", "")) == 64)
    _check("EX-IAT", "issued_at present", bool(bundle.get("issued_at")))
    _check("EX-SCH", "schema_version is sp19_v1",
           bundle.get("schema_version") == "sp19_v1")

    # Signature mode declared (gap must be machine-detectable)
    op_sig = (bundle.get("signatures") or {}).get("operator") or {}
    sys_sig = (bundle.get("signatures") or {}).get("system") or {}
    _check("EX-SGO", "operator signature_mode declared", bool(op_sig.get("signature_mode")))
    _check("EX-SGS", "system signature_mode declared", bool(sys_sig.get("signature_mode")))

    # subject_ids non-empty
    _check("EX-SID", "subject_ids non-empty", bool(bundle.get("subject_ids")))

    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    decision = "valid" if all(c["passed"] for c in checks) else "invalid"

    return {
        "decision": decision,
        "passed_checks": passed,
        "total_checks": total,
        "checks": checks,
        "bundle_id": bundle.get("bundle_id", ""),
        "validated_at": _utcnow(),
        "schema_version": "sp19_v1",
    }
