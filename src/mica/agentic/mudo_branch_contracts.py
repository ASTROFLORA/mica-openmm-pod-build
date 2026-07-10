from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from .events import MUDOReceiptReady


MUDOBranchType = Literal["canonical", "candidate", "failed", "rejected", "superseded"]
MUDO_BRANCH_TYPES: tuple[str, ...] = ("canonical", "candidate", "failed", "rejected", "superseded")
MUDO_NONCANONICAL_BRANCH_TYPES: tuple[str, ...] = ("candidate", "failed", "rejected", "superseded")


class MUDOBranchContractError(ValueError):
    """Typed contract error for P5 MUDO branch receipt ingestion."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MUDOBranchReceipt(BaseModel):
    """Refs-only receipt for noncanonical MUDO branch proposal ingestion.

    This object is not a durable MUDO commit. It is the P5 contract that lets
    debate/proposal surfaces describe branch intent before the canonical MUDO
    writer persists anything.
    """

    schema_id: str = "mica.mudo.branch_receipt.v1"
    mudo_id: str = Field(..., min_length=1)
    mudo_id_status: Literal["resolved", "pending_resolution"] = "resolved"
    branch_id: str = Field(..., min_length=1)
    branch_type: MUDOBranchType = "candidate"
    commit_hash: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    source_surface: str = Field(..., min_length=1)
    receipt_kind: str = Field(..., min_length=1)
    correlation_id: str = ""
    study_id: str = Field(..., min_length=1)
    protocol_ref: str = Field(..., min_length=1)
    workspace_id: str = ""
    owner_user_id: str = ""
    input_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    canonical_branch_mutation_allowed: bool = False
    provenance_authority: Literal["mudo_subscriber_pending_persistence"] = "mudo_subscriber_pending_persistence"


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_mudo_branch_type(value: str | None) -> MUDOBranchType:
    branch_type = str(value or "candidate").strip().lower()
    if branch_type not in MUDO_BRANCH_TYPES:
        raise MUDOBranchContractError("invalid_branch_type", f"Unsupported MUDO branch type: {branch_type}")
    return branch_type  # type: ignore[return-value]


def make_mudo_branch_id(*, branch_type: str, source_surface: str, correlation_id: str, commit_hash: str) -> str:
    seed = f"{branch_type}|{source_surface}|{correlation_id}|{commit_hash}"
    return f"mub_{branch_type}_{canonical_json_sha256(seed)[:24]}"


def make_pending_mudo_id(*, study_id: str, workspace_id: str) -> str:
    seed = f"{workspace_id}|{study_id}"
    return f"mudo_pending_{canonical_json_sha256(seed)[:24]}"


def make_mudo_branch_idempotency_key(*, mudo_id: str, branch_id: str, commit_hash: str) -> str:
    return f"mudo:{mudo_id}:branch:{branch_id}:commit:{commit_hash}"


def branch_receipt_from_mudo_event(event: MUDOReceiptReady) -> MUDOBranchReceipt:
    study_id = str(getattr(event, "study_id", "") or "").strip()
    protocol_ref = str(getattr(event, "protocol_ref", "") or "").strip()
    if not study_id:
        raise MUDOBranchContractError("missing_study_id", "MUDO branch receipt requires study_id")
    if not protocol_ref:
        raise MUDOBranchContractError("missing_protocol_ref", "MUDO branch receipt requires protocol_ref")

    payload = dict(getattr(event, "receipt_payload", {}) or {})
    branch_type = validate_mudo_branch_type(payload.get("branch_type"))
    commit_hash = str(payload.get("commit_hash") or "").strip()
    if not commit_hash:
        commit_hash = canonical_json_sha256(
            {
                "receipt_kind": getattr(event, "receipt_kind", ""),
                "source_surface": getattr(event, "source_surface", ""),
                "study_id": study_id,
                "protocol_ref": protocol_ref,
                "artifact_refs": list(getattr(event, "artifact_refs", []) or []),
                "evidence_refs": list(getattr(event, "evidence_refs", []) or []),
                "receipt_payload": payload,
            }
        )

    branch_id = str(payload.get("branch_id") or "").strip()
    if not branch_id:
        branch_id = make_mudo_branch_id(
            branch_type=branch_type,
            source_surface=str(getattr(event, "source_surface", "") or ""),
            correlation_id=str(getattr(event, "correlation_id", "") or ""),
            commit_hash=commit_hash,
        )

    workspace_id = str(getattr(event, "workspace_id", "") or "").strip()
    mudo_id = str(payload.get("mudo_id") or "").strip()
    mudo_id_status: Literal["resolved", "pending_resolution"] = "resolved"
    if not mudo_id:
        mudo_id = make_pending_mudo_id(study_id=study_id, workspace_id=workspace_id)
        mudo_id_status = "pending_resolution"

    return MUDOBranchReceipt(
        mudo_id=mudo_id,
        mudo_id_status=mudo_id_status,
        branch_id=branch_id,
        branch_type=branch_type,
        commit_hash=commit_hash,
        idempotency_key=make_mudo_branch_idempotency_key(
            mudo_id=mudo_id,
            branch_id=branch_id,
            commit_hash=commit_hash,
        ),
        source_surface=str(getattr(event, "source_surface", "") or "unknown_surface").strip() or "unknown_surface",
        receipt_kind=str(getattr(event, "receipt_kind", "") or "unknown_receipt").strip() or "unknown_receipt",
        correlation_id=str(getattr(event, "correlation_id", "") or "").strip(),
        study_id=study_id,
        protocol_ref=protocol_ref,
        workspace_id=workspace_id,
        owner_user_id=str(getattr(event, "owner_user_id", "") or "").strip(),
        input_refs=[str(ref).strip() for ref in list(getattr(event, "input_refs", []) or []) if str(ref).strip()],
        artifact_refs=[str(ref).strip() for ref in list(getattr(event, "artifact_refs", []) or []) if str(ref).strip()],
        evidence_refs=[str(ref).strip() for ref in list(getattr(event, "evidence_refs", []) or []) if str(ref).strip()],
        reason_codes=[str(code).strip() for code in list(payload.get("reason_codes") or []) if str(code).strip()],
        canonical_branch_mutation_allowed=branch_type == "canonical",
    )
