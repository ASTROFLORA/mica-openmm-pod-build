"""src/mica/provenance/receipts.py — ASTROFLORA mirror shim.

Vendored minimal subset of upstream `mica.provenance.receipts` so the CG
lane modules can be imported and exercised in the ASTROFLORA mirror
without read access to the upstream private repo. Schema mirrors the
upstream Pydantic v2 contracts used by `Martinize2Adapter`,
`INSANEAdapter`, `TopologyPreprocessor`, `GeometryAudit`, and
`OverlapRemediation`.

Schema source: upstream `src/mica/provenance/receipts.py` (juaness38/MICA-ultimate)
  - ReceiptRefs  : policy_refs, output_refs, artifact_refs (lists of str)
  - ReceiptHashes: request_hash (str), output_hash (Optional[str]), content_hash (str)
  - GatePayload  : gate_name, decision, reason_codes, max_allowed_tier, provider_job_created
  - ServerlessPayload: model_ref, model_revision_ref, modal_app, modal_function, etc.
  - ReceiptCore  : the canonical receipt envelope (see fields below)

This shim does NOT import any non-stdlib module except pydantic, which
is already in the GHA install list (see submit-cg-martini-job.yml
`Install Python deps`). The CG adapters' usage is field-compatible with
this shim:
  - ``ReceiptCore(receipt_id=..., kind=..., status=..., workspace_id=...,
    actor_id=..., operation_name=..., refs=ReceiptRefs(...),
    hashes=ReceiptHashes(...), started_at=..., ended_at=..., trace_id=...,
    payload=...)``
  - ``payload=payload.model_dump()`` -> a JSON-able dict.

Upstream provenance semantics (canonical provenance events, MUDO
ingestion, gate policy) are NOT vendored; this shim is a structural
envelope only.
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class ReceiptRefs(BaseModel):
    """References emitted alongside a receipt."""

    policy_refs: List[str] = Field(default_factory=list)
    output_refs: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)


class ReceiptHashes(BaseModel):
    """Content hashes for a receipt's request/output/content."""

    request_hash: str
    output_hash: Optional[str] = None
    content_hash: str


class GatePayload(BaseModel):
    """Payload for a Quetzal gate decision receipt."""

    gate_name: str
    decision: str
    reason_codes: List[str] = Field(default_factory=list)
    max_allowed_tier: Optional[str] = None
    provider_job_created: bool = False


class ServerlessPayload(BaseModel):
    """Payload for a serverless / provider inference receipt."""

    model_ref: str
    model_revision_ref: str
    modal_app: str
    modal_function: str
    input_schema_version: str = "v1"
    output_schema_version: str = "v1"
    provider_job_id: Optional[str] = None


class ReceiptCore(BaseModel):
    """Canonical receipt envelope.

    The CG lane attaches a payload-specific Pydantic model (e.g.
    ``Martinize2Payload``, ``INSANEBuildPayload``, ``CGTopologyPreprocessPayload``)
    via ``payload=payload.model_dump()`` so the downstream consumer
    reads a JSON-able dict — the schema of ``payload`` is intentionally
    ``Any`` here to allow lane-specific payloads.
    """

    receipt_id: str
    kind: str
    status: str
    workspace_id: str
    actor_id: str
    operation_name: str
    refs: ReceiptRefs
    hashes: ReceiptHashes
    started_at: str
    ended_at: str
    trace_id: str
    payload: Any  # GatePayload | ServerlessPayload | lane-specific Pydantic model (dumped)