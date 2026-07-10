"""KB_INTEGRATION_CONTRACT_V1 Pydantic models.

Versioned at 1.0.0. Any breaking change (dim, field semantics, response
shape) bumps the version. The contract_ref field on every record embeds
the version string so downstream consumers can detect mismatches.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, get_args
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


CONTRACT_VERSION: Literal["v1.0.0"] = "v1.0.0"


# Single source of truth for embedding dim across MICA.
# Every consumer (BiolinkBERT descriptor, Neon schema, contract, gateway)
# MUST import this literal — never hardcode 768 or 1536 anywhere.
KBEmbeddingDim = Literal[1024]
KB_EMBEDDING_DIM: int = 1024  # materialised runtime value (Literal is type-only)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KBProvenanceReceipt(BaseModel):
    """Where the document came from. Required for every persisted doc."""

    model_config = ConfigDict(extra="forbid")

    receipt_urn: str = Field(..., description="urn:mica:provenance:<uuid>")
    source_url: Optional[str] = Field(None, description="Public URL or DOI-resolved URL")
    source_doi: Optional[str] = Field(None, description="DOI string without 'https://doi.org/' prefix")
    retrieved_at: str = Field(default_factory=_utcnow_iso)
    retrieval_method: str = Field(default="seed_source", description="seed_source|literature_search|user_input")
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION

    @field_validator("receipt_urn")
    @classmethod
    def _urn_must_match(cls, v: str) -> str:
        if not v.startswith("urn:mica:provenance:"):
            raise ValueError("receipt_urn must start with urn:mica:provenance:")
        return v


class KBDocumentEmbedding(BaseModel):
    """1024-dim L2-normalised embedding for one document."""

    model_config = ConfigDict(extra="forbid")

    doc_id: UUID
    embedding: list[float] = Field(..., min_length=1024, max_length=1024)
    dim: KBEmbeddingDim = 1024
    l2_norm: float = Field(..., ge=0.99, le=1.01, description="Must be ~1.0 (L2-normalised)")
    model_id: Literal["biolinkbert-large", "michiyasunaga/BioLinkBERT-large"] = "biolinkbert-large"
    embed_receipt_urn: str = Field(..., description="urn:mica:embed:<uuid>")
    embedded_at: str = Field(default_factory=_utcnow_iso)
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION

    @field_validator("embedding")
    @classmethod
    def _check_dim(cls, v: list[float]) -> list[float]:
        if len(v) != 1024:
            raise ValueError(f"embedding dim must be exactly 1024, got {len(v)}")
        if any(not (-2.0 < x < 2.0) for x in v):
            raise ValueError("embedding values out of expected range")
        return v


class KBDocumentRecord(BaseModel):
    """A document staged for KB persistence with its embedding and provenance."""

    model_config = ConfigDict(extra="forbid")

    doc_id: UUID = Field(default_factory=uuid4)
    content: str = Field(..., min_length=1, max_length=200_000)
    source_url: Optional[str] = None
    source_doi: Optional[str] = None
    content_hash: str = Field(..., description="SHA-256 of canonical content for idempotency")
    mudo_id: UUID = Field(default_factory=uuid4, description="Multi-tenant unit of data ownership")
    branch_id: UUID = Field(default_factory=uuid4)
    created_at: str = Field(default_factory=_utcnow_iso)
    provenance: KBProvenanceReceipt
    embedding: KBDocumentEmbedding
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION


class KBIngestRequest(BaseModel):
    """kb.ingest input: a list of documents ready to be embedded and stored."""

    model_config = ConfigDict(extra="forbid")

    kb_id: UUID = Field(..., description="Target KB identifier")
    documents: list[KBDocumentRecord] = Field(..., min_length=1, max_length=500)
    dry_run: bool = Field(default=False, description="If true, embed but do not persist")
    promote_to_timescale: bool = Field(default=False, description="If true and Timescale active, propagate edges")
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION


class KBIngestResponse(BaseModel):
    """kb.ingest output: receipts for each document + degraded_reason if any."""

    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    ingested_count: int = Field(..., ge=0)
    skipped_duplicates: int = Field(default=0, ge=0)
    provenance_receipts: list[str] = Field(default_factory=list)
    embed_receipts: list[str] = Field(default_factory=list)
    promotion_receipts: list[str] = Field(default_factory=list)
    degraded_reason: Optional[str] = Field(None, description="Set if any backing store or embed stage failed")
    embedding_dim: KBEmbeddingDim = 1024
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION


class KBSemanticSearchRequest(BaseModel):
    """kb.semantic_search input: query text or pre-computed query embedding."""

    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    query_text: Optional[str] = Field(None, min_length=1, max_length=10_000)
    query_embedding: Optional[list[float]] = Field(
        None, min_length=1024, max_length=1024,
        description="Pre-computed 1024-dim query embedding (mutually exclusive with query_text)",
    )
    top_k: int = Field(default=10, ge=1, le=100)
    min_similarity: float = Field(default=0.0, ge=-1.0, le=1.0)
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION

    @field_validator("query_embedding")
    @classmethod
    def _check_dim(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is not None and len(v) != 1024:
            raise ValueError(f"query_embedding dim must be exactly 1024, got {len(v)}")
        return v


class KBSearchHit(BaseModel):
    """One search hit with score, content preview, and provenance."""

    model_config = ConfigDict(extra="forbid")

    doc_id: UUID
    content_preview: str = Field(..., max_length=500)
    similarity: float = Field(..., ge=-1.0, le=1.0)
    source_url: Optional[str] = None
    source_doi: Optional[str] = None
    provenance_receipt_urn: str
    embed_receipt_urn: str
    content_hash: str
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION


class KBSemanticSearchResponse(BaseModel):
    """kb.semantic_search output: ranked hits + degraded_reason."""

    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    hits: list[KBSearchHit]
    embed_receipt_urn: str
    degraded_reason: Optional[str] = None
    embedding_dim: KBEmbeddingDim = 1024
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION


class KBPromotionReceipt(BaseModel):
    """Receipt for a Neon→Timescale edge promotion (Tier-1 → Tier-2)."""

    model_config = ConfigDict(extra="forbid")

    receipt_urn: str = Field(..., description="urn:mica:promotion:<uuid>")
    from_tier: Literal["neon_t1"] = "neon_t1"
    to_tier: Literal["timescale_t2", "neon_t1"] = "timescale_t2"
    idempotency_key: str = Field(..., description="(mudo_id, branch_id, edge_id)")
    promoted_count: int = Field(..., ge=0)
    skipped_duplicates: int = Field(default=0, ge=0)
    promoted_at: str = Field(default_factory=_utcnow_iso)
    contract_ref: Literal["v1.0.0"] = CONTRACT_VERSION