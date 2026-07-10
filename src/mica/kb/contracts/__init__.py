"""KB_INTEGRATION_CONTRACT_V1 — single source of truth for KB surface shapes.

This package defines the Pydantic models that gate `kb.ingest` and
`kb.semantic_search` request/response shapes, and the canonical
KBEmbeddingDim literal that propagates from the BiolinkBERT Modal descriptor
through to the Neon vector column.

Design constraints:

- Single embedding dim (1024) — no 768, no 1536. Anything else is a bug.
- L2-normalised embeddings only. Unit-norm verified before persistence.
- Idempotency key: (mudo_id, branch_id, content_hash).
- Provenance required on every persisted document (source_url OR source_doi).
- Receipts (provenance_urn, embed_receipt_urn) are first-class fields, not
  decorative metadata.
"""

from .kb_integration_contract_v1 import (
    KBEmbeddingDim,
    KB_EMBEDDING_DIM,
    KBIngestRequest,
    KBIngestResponse,
    KBSemanticSearchRequest,
    KBSemanticSearchResponse,
    KBDocumentEmbedding,
    KBDocumentRecord,
    KBSearchHit,
    KBProvenanceReceipt,
    KBPromotionReceipt,
    CONTRACT_VERSION,
)

__all__ = [
    "KBEmbeddingDim",
    "KB_EMBEDDING_DIM",
    "KBIngestRequest",
    "KBIngestResponse",
    "KBSemanticSearchRequest",
    "KBSemanticSearchResponse",
    "KBDocumentEmbedding",
    "KBDocumentRecord",
    "KBSearchHit",
    "KBProvenanceReceipt",
    "KBPromotionReceipt",
    "CONTRACT_VERSION",
]