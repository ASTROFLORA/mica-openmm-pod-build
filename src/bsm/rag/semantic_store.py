"""BSM Semantic Store facade bridging legacy RAG indexer with unified config."""
from __future__ import annotations

import math
import os
import time
import heapq
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple


@dataclass
class BSMDocument:
    """Container for indexed artifacts in the semantic store."""

    doc_id: str
    text: str
    vector: List[float]
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class BSMSemanticWeights:
    """Ranking weights for semantic, recency and source signals."""

    similarity: float = 1.0
    recency: float = 1.0
    source: float = 1.0

    @classmethod
    def from_environment(cls) -> "BSMSemanticWeights":
        """Load weighting configuration from environment variables."""

        def _value(name: str, fallback: float) -> float:
            raw = os.getenv(name)
            if raw is None:
                return fallback
            try:
                return float(raw)
            except ValueError:
                return fallback

        return cls(
            similarity=_value("BSM_RAG_WEIGHT_SIM", _value("RAG_WEIGHT_SIM", 1.0)),
            recency=_value("BSM_RAG_WEIGHT_RECENCY", _value("RAG_WEIGHT_RECENCY", 1.0)),
            source=_value("BSM_RAG_WEIGHT_SOURCE", _value("RAG_WEIGHT_SOURCE", 1.0)),
        )


class BSMSemanticStore:
    """Unified semantic index used by BSM RAG orchestration."""

    def __init__(self) -> None:
        self._documents: Dict[str, BSMDocument] = {}
        self._dimension: Optional[int] = None
        self._weights = BSMSemanticWeights.from_environment()

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------
    def add_or_update(
        self,
        doc_id: str,
        text: str,
        vector: List[float],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or replace a document in the semantic store."""

        if self._dimension is None:
            self._dimension = len(vector)
        elif len(vector) != self._dimension:
            raise ValueError("Embedding dimension mismatch detected")

        self._documents[doc_id] = BSMDocument(
            doc_id=doc_id,
            text=text,
            vector=vector,
            metadata=metadata or {},
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def _cosine_similarity(self, lhs: List[float], rhs: List[float]) -> float:
        dot = 0.0
        norm_l = 0.0
        norm_r = 0.0
        for l, r in zip(lhs, rhs):
            dot += l * r
            norm_l += l * l
            norm_r += r * r
        if norm_l == 0 or norm_r == 0:
            return 0.0
        return dot / (math.sqrt(norm_l) * math.sqrt(norm_r))

    def search(
        self,
        query_vec: List[float],
        *,
        k: int = 5,
        recency_half_life_s: float = 3600.0,
    ) -> List[Tuple[float, BSMDocument]]:
        """Retrieve the top-k documents according to the configured weights."""

        now = time.time()
        weights = self._weights
        scored: List[Tuple[float, BSMDocument]] = []

        for doc in self._documents.values():
            similarity = self._cosine_similarity(query_vec, doc.vector)

            age = now - doc.created_at
            recency_weight = math.exp(-age / recency_half_life_s * 0.69314718056)

            source_weight = 1.0
            source = doc.metadata.get("source") if doc.metadata else None
            if source is not None:
                env_key = f"BSM_RAG_WEIGHT_SOURCE_{source.upper()}"
                try:
                    source_weight += float(os.getenv(env_key, "0.0"))
                except ValueError:
                    source_weight += 0.0
            score = (
                similarity * weights.similarity
                * (recency_weight ** weights.recency)
                * (source_weight ** weights.source)
            )
            scored.append((score, doc))

        if not scored:
            return []

        return heapq.nlargest(k, scored, key=lambda pair: pair[0])

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------
    @property
    def dimension(self) -> Optional[int]:
        """Return the dimensionality of stored embeddings."""

        return self._dimension

    @property
    def size(self) -> int:
        """Return number of indexed documents."""

        return len(self._documents)


GLOBAL_SEMANTIC_INDEX = BSMSemanticStore()

__all__ = ["BSMSemanticStore", "BSMDocument", "BSMSemanticWeights", "GLOBAL_SEMANTIC_INDEX"]
