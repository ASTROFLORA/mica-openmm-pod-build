"""SMIC + GCS audit lane — read-only probe over protocol-node receipts.

This module satisfies Packet 7B proof #6 from
`.mica/programs/INSTITUTIONAL_SUPERNOVA/SYSTEM_OS_CLOSURE_MASTER_ROADMAP_2026-05-16.md`:
a non-mutating consumer that scans recent driver / sandbox / Dynamo session
node receipts and emits structured findings against the SMIC metric registry
and the GCS workspace binding/promotion receipt contracts.

Design contract
---------------
* Pure: no I/O, no remediation, no external calls.
* Structured: every finding is a Pydantic v2 model with stable field names.
* Compositional: callers supply receipts; this lane has no opinion about
  storage (Redis / Timescale / filesystem).
* Schema-aware: validates the two GCS receipt schemas declared in
  `src/mica/api_v1/routers/user_bucket.py`.
* Registry-aware: validates SMIC module_key against the live module
  registry exposed by `src/mica/api_v1/routers/smic.py`.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

# Canonical schema constants — re-import (not redefine) to keep the audit
# lane locked to the same authority as the runtime emitters.
from mica.api_v1.routers.user_bucket import (
    ARTIFACT_PROMOTION_RECEIPT_SCHEMA,
    DEMO_WORKSPACE_ENTITLEMENT_CODE,
    WORKSPACE_BINDING_RECEIPT_SCHEMA,
)

_SMIC_SURFACE = "smic"
_WORKSPACE_SURFACES = frozenset(
    {"mica_user_workspace", "gcs_user_workspace", "user_bucket", "workspace_storage"}
)
_WORKSPACE_DURABILITY_CLASSES = frozenset(
    {"ephemeral_returned", "workspace_durable", "workspace_durable_with_signed_url"}
)

# Severity vocabulary kept small and stable for downstream filtering.
_SEV_INFO = "info"
_SEV_WARN = "warn"
_SEV_ERROR = "error"


class SMICGCSAuditFinding(BaseModel):
    """Single audit finding emitted by the SMIC + GCS audit lane."""

    model_config = ConfigDict(extra="forbid")

    gap_type: str = Field(
        ...,
        description=(
            "Stable identifier for the kind of gap detected. One of: "
            "'smic_unknown_module', 'smic_missing_module_key', "
            "'smic_missing_command_intent', 'workspace_missing_clerk_identity', "
            "'workspace_invalid_entitlement', 'workspace_schema_drift', "
            "'workspace_invalid_durability_class', "
            "'workspace_missing_receipt', 'actor_surface_unrecognized'."
        ),
    )
    caller_lane: str = Field(
        ...,
        description="The actor_surface that produced the receipt (e.g., 'smic', 'mica_user_workspace').",
    )
    module_key: str | None = Field(
        default=None,
        description="SMIC module key when applicable (e.g., 'rmsd'); None for workspace findings.",
    )
    node_id: str = Field(..., description="Protocol node identifier.")
    protocol_id: str = Field(..., description="Protocol document identifier.")
    actor_id: str = Field(default="", description="actor_id from the receipt, if available.")
    schema_id_observed: str | None = Field(
        default=None,
        description="Schema id seen in the receipt payload, if a receipt was inspected.",
    )
    schema_id_expected: str | None = Field(
        default=None,
        description="Expected schema id for the surface, if applicable.",
    )
    remediation_pointer: str = Field(
        ...,
        description=(
            "Stable path or identifier pointing to where the gap must be repaired. "
            "Always a code path or canonical doc anchor — never a free-text instruction."
        ),
    )
    severity: str = Field(
        default=_SEV_WARN,
        description="One of 'info', 'warn', 'error'.",
    )


def _coerce_receipt(receipt: Any) -> Mapping[str, Any]:
    """Accept either a Pydantic ProtocolNodeReceipt or a plain mapping."""
    if isinstance(receipt, Mapping):
        return receipt
    dump = getattr(receipt, "model_dump", None)
    if callable(dump):
        return dump()
    raise TypeError(f"unsupported receipt type: {type(receipt).__name__}")


def _smic_module_registry_keys() -> frozenset[str]:
    """Late-binding call to the SMIC registry — avoids import-time side effects."""
    from mica.api_v1.routers import smic as _smic_router

    return frozenset((_smic_router._runtime_module_registry() or {}).keys())  # noqa: SLF001


def _audit_smic_receipt(payload: Mapping[str, Any]) -> list[SMICGCSAuditFinding]:
    findings: list[SMICGCSAuditFinding] = []
    state_after = payload.get("state_after") or {}
    module_key = str(state_after.get("module_key") or "").strip() or None
    protocol_id = str(payload.get("protocol_id") or "")
    node_id = str(payload.get("node_id") or "")
    actor_id = str(payload.get("actor_id") or "")
    base = dict(
        caller_lane=_SMIC_SURFACE,
        node_id=node_id,
        protocol_id=protocol_id,
        actor_id=actor_id,
        schema_id_observed=str(payload.get("schema_id") or "") or None,
        schema_id_expected="mica.receipts.node.v1",
        module_key=module_key,
    )

    if not module_key:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="smic_missing_module_key",
                remediation_pointer="src/mica/api_v1/routers/smic.py::execute_protocol_smic_action",
                severity=_SEV_ERROR,
            )
        )
        return findings

    registered = _smic_module_registry_keys()
    if module_key not in registered:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="smic_unknown_module",
                remediation_pointer="src/mica/api_v1/routers/smic.py::_MODULE_REGISTRY",
                severity=_SEV_ERROR,
            )
        )

    command_intent = state_after.get("command_intent")
    if not command_intent:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="smic_missing_command_intent",
                remediation_pointer="src/mica/api_v1/routers/smic.py::execute_protocol_smic_action#evidence_refs",
                severity=_SEV_WARN,
            )
        )
    return findings


def _audit_workspace_receipt(payload: Mapping[str, Any]) -> list[SMICGCSAuditFinding]:
    findings: list[SMICGCSAuditFinding] = []
    state_after = payload.get("state_after") or {}
    receipt_family = str(state_after.get("receipt_family") or "")
    binding_receipt = state_after.get("binding_receipt") or {}
    promotion_receipt = state_after.get("promotion_receipt") or {}
    inner_receipt = binding_receipt or promotion_receipt
    expected_schema = receipt_family or None
    observed_schema = str(inner_receipt.get("schema_id") or "") if isinstance(inner_receipt, Mapping) else None

    protocol_id = str(payload.get("protocol_id") or "")
    node_id = str(payload.get("node_id") or "")
    actor_id = str(payload.get("actor_id") or "")
    caller_lane = str(payload.get("actor_surface") or "mica_user_workspace")
    base = dict(
        caller_lane=caller_lane,
        node_id=node_id,
        protocol_id=protocol_id,
        actor_id=actor_id,
        schema_id_observed=observed_schema,
        schema_id_expected=expected_schema,
        module_key=None,
    )

    if not isinstance(inner_receipt, Mapping) or not inner_receipt:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="workspace_missing_receipt",
                remediation_pointer="src/mica/api_v1/routers/user_bucket.py::execute_protocol_workspace_action",
                severity=_SEV_ERROR,
            )
        )
        return findings

    # Schema drift detection: inner schema_id MUST match one of the two known
    # workspace receipt families, and must match the declared receipt_family
    # when one is present.
    known_schemas = {WORKSPACE_BINDING_RECEIPT_SCHEMA, ARTIFACT_PROMOTION_RECEIPT_SCHEMA}
    if observed_schema not in known_schemas:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="workspace_schema_drift",
                remediation_pointer="src/mica/api_v1/routers/user_bucket.py#WORKSPACE_BINDING_RECEIPT_SCHEMA",
                severity=_SEV_ERROR,
            )
        )
    elif expected_schema and expected_schema != observed_schema:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="workspace_schema_drift",
                remediation_pointer="src/mica/api_v1/routers/user_bucket.py::execute_protocol_workspace_action#receipt_family",
                severity=_SEV_ERROR,
            )
        )

    clerk_user_id = str(inner_receipt.get("clerk_user_id") or "").strip()
    clerk_email = str(inner_receipt.get("clerk_email") or "").strip()
    if not clerk_user_id or not clerk_email:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="workspace_missing_clerk_identity",
                remediation_pointer="src/mica/api_v1/routers/user_bucket.py::_resolve_workspace_subject",
                severity=_SEV_ERROR,
            )
        )

    entitlement_code = str(inner_receipt.get("entitlement_code") or "").strip()
    # Promotion receipts intentionally do not carry the entitlement code
    # (it lives on the binding receipt).  Only validate when present.
    if entitlement_code and entitlement_code != DEMO_WORKSPACE_ENTITLEMENT_CODE:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="workspace_invalid_entitlement",
                remediation_pointer="src/mica/api_v1/routers/user_bucket.py::_validate_entitlement_code",
                severity=_SEV_ERROR,
            )
        )

    durability_class = str(inner_receipt.get("durability_class") or "").strip()
    if durability_class and durability_class not in _WORKSPACE_DURABILITY_CLASSES:
        findings.append(
            SMICGCSAuditFinding(
                **base,
                gap_type="workspace_invalid_durability_class",
                remediation_pointer="src/mica/api_v1/routers/user_bucket.py::promote_workspace_artifact_payload#durability_class",
                severity=_SEV_ERROR,
            )
        )
    return findings


def audit_protocol_node_receipts(
    receipts: Iterable[Any],
) -> list[SMICGCSAuditFinding]:
    """Run the SMIC + GCS audit lane over a collection of node receipts.

    Accepts either Pydantic `ProtocolNodeReceipt` instances or plain dict
    payloads (model_dump output).  Returns a flat list of findings; an empty
    list means the receipts under audit are clean against the current
    SMIC registry and the workspace receipt contracts.

    The lane is intentionally read-only — it produces findings, never side
    effects.  Callers (driver, sandbox, Dynamo session indexer) decide
    whether to surface them, file them, or remediate.
    """
    findings: list[SMICGCSAuditFinding] = []
    for raw in receipts:
        payload = _coerce_receipt(raw)
        actor_surface = str(payload.get("actor_surface") or "").strip().lower()
        if actor_surface == _SMIC_SURFACE:
            findings.extend(_audit_smic_receipt(payload))
        elif actor_surface in _WORKSPACE_SURFACES:
            findings.extend(_audit_workspace_receipt(payload))
        else:
            findings.append(
                SMICGCSAuditFinding(
                    gap_type="actor_surface_unrecognized",
                    caller_lane=actor_surface or "unknown",
                    node_id=str(payload.get("node_id") or ""),
                    protocol_id=str(payload.get("protocol_id") or ""),
                    actor_id=str(payload.get("actor_id") or ""),
                    schema_id_observed=str(payload.get("schema_id") or "") or None,
                    schema_id_expected=None,
                    module_key=None,
                    remediation_pointer="src/mica/audit/smic_gcs_audit.py::audit_protocol_node_receipts",
                    severity=_SEV_INFO,
                )
            )
    return findings
