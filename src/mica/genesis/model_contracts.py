from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from mica.storage.workspace_artifact_contract import ClaimBoundary


SupportedTaskKind = Literal[
    "sequence_generation",
    "sequence_design",
    "inverse_folding",
    "structure_prediction",
    "complex_prediction",
    "embedding",
    "scoring",
    "conditioning",
    "dataset_filtering",
]

CapabilityEvidenceBacking = Literal["code-backed", "runtime-backed", "doc-backed", "future"]
EvidenceGateStatus = Literal["passed", "partial", "blocked", "not_applicable"]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GenesisModelCapability:
    model_id: str
    display_name: str
    model_family: str
    provider: str
    runtime_kind: str
    supported_tasks: List[SupportedTaskKind]
    input_contract: Dict[str, Any]
    output_contract: Dict[str, Any]
    artifact_policy: Dict[str, Any]
    gcs_workspace_required: bool
    estimated_cost_class: str
    secrets_required: List[str]
    preflight_supported: bool
    smoke_supported: bool
    production_ready: bool
    blocker: str
    readiness_state: str
    evidence_backing: CapabilityEvidenceBacking
    code_paths: List[str] = field(default_factory=list)
    image_ref: str = ""
    dockerfile_ref: str = ""
    ghcr_ref: str = ""
    last_evidence_packet: str = ""
    receipt_schema: Dict[str, Any] = field(default_factory=dict)
    cost_runtime_policy: Dict[str, Any] = field(default_factory=dict)
    fallback_policy: Dict[str, Any] = field(default_factory=dict)
    evidence_gate: Dict[str, Any] = field(default_factory=dict)
    product_exposure: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GenesisModelExecutionReceipt:
    model_id: str
    action: Literal["preflight", "smoke"]
    status: str
    readiness_state: str
    provider: str
    runtime_kind: str
    production_ready: bool
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_secret_logged: bool = False
    remote_probe: Dict[str, Any] = field(default_factory=dict)
    artifact_manifest: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GenesisModelOutputArtifact(BaseModel):
    artifact_id: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    input_ref: str = Field(..., min_length=1)
    output_ref: str = Field(..., min_length=1)
    gcs_uri: Optional[str] = None
    local_path: Optional[str] = None
    sha256: str = Field(..., min_length=1)
    size_bytes: int = Field(..., ge=0)
    protein_context_ref: str = Field(..., min_length=1)
    claim_boundary: ClaimBoundary
    evidence_gate_status: EvidenceGateStatus
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_storage_boundary(self) -> "GenesisModelOutputArtifact":
        if self.claim_boundary == "gcs_production" and not self.gcs_uri:
            raise ValueError("gcs_production artifacts require gcs_uri")
        if self.claim_boundary == "local_non_production" and not self.local_path:
            raise ValueError("local_non_production artifacts require local_path")
        if self.claim_boundary == "blocked_missing_gcs_uri" and self.gcs_uri:
            raise ValueError("blocked_missing_gcs_uri artifacts must not claim gcs_uri")
        return self


def build_genesis_output_artifact_contract() -> Dict[str, Any]:
    return {
        "schema_version": "genesis_model_output_artifact_contract_v1",
        "model_schema": GenesisModelOutputArtifact.model_json_schema(),
        "artifact_kinds": [
            "generated_sequence.fasta",
            "designed_sequence.fasta",
            "sequence_scores.json",
            "structure_prediction.pdb",
            "structure_prediction.cif",
            "complex_prediction.pdb",
            "embeddings.npz",
            "embeddings.json",
            "model_receipt.json",
            "protein_portrait.json",
            "lmp_preset.xml",
            "mol_lsp_context.json",
        ],
        "claim_boundaries": ["gcs_production", "local_non_production", "blocked_missing_gcs_uri"],
        "evidence_gate_statuses": ["passed", "partial", "blocked", "not_applicable"],
        "workspace_binding": {
            "gcs_workspace_required": True,
            "canonical_contract": "mica.storage.workspace_artifact_contract.WorkspaceArtifactContract",
        },
    }
