"""Unified orchestrator for BSM Retrieval-Augmented Generation pipelines."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .drift_monitor import DriftReference

try:
    from embedding.active import embed_sequence  # type: ignore
except Exception:  # pragma: no cover
    try:
        from esm2_provider import embed_sequence  # type: ignore
    except Exception:  # pragma: no cover
        def embed_sequence(_: str) -> List[float]:  # type: ignore
            return [0.0]

from .artifact_ingestion import BSMArtifactIngestionService
from .context_assembly import assemble_context
from .graph_bridge import GraphRAGBridge
from .semantic_store import GLOBAL_SEMANTIC_INDEX, BSMSemanticStore


@dataclass
class SearchResult:
    """Lightweight representation of semantic search hits."""

    doc_id: str
    score: float
    source: Optional[str]
    preview: str
    metadata: Dict[str, Any]


class BSMRAGOrchestrator:
    """High-level orchestrator bridging semantic RAG, GraphRAG, and MCP tools."""

    def __init__(
        self,
        store: Optional[BSMSemanticStore] = None,
        ingestion_service: Optional[BSMArtifactIngestionService] = None,
        graph_bridge: Optional[GraphRAGBridge] = None,
    ) -> None:
        self.store = store or GLOBAL_SEMANTIC_INDEX
        self.ingestion = ingestion_service or BSMArtifactIngestionService(store=self.store)
        self.graph = graph_bridge or GraphRAGBridge()
        self.drift_reference = DriftReference()

    # ------------------------------------------------------------------
    async def ensure_background_ingest(self, interval_s: float = 300.0) -> None:
        await self.ingestion.maybe_background_ingest(interval_s=interval_s)

    def search(self, query: str, *, k: int = 5) -> List[SearchResult]:
        vector = embed_sequence(query)
        hits = self.store.search(vector, k=k)
        results: List[SearchResult] = []
        for score, doc in hits:
            snippet = doc.text[:200]
            source = doc.metadata.get("source") if doc.metadata else None
            results.append(
                SearchResult(
                    doc_id=doc.doc_id,
                    score=round(score, 6),
                    source=source,
                    preview=snippet,
                    metadata=doc.metadata,
                )
            )
        return results

    async def hybrid_answer(
        self,
        query: str,
        *,
        k: int = 5,
        include_graph: bool = True,
    ) -> Dict[str, Any]:
        semantic_hits = self.search(query, k=k)
        context_payload = assemble_context(
            [hit.preview for hit in semantic_hits],
            embed_sequence,
            self.drift_reference,
        )

        graph_payload: Optional[Dict[str, Any]] = None
        if include_graph:
            graph_payload = await self.graph.run_query(query)

        return {
            "query": query,
            "semantic_hits": [hit.__dict__ for hit in semantic_hits],
            "context": context_payload,
            "graph": graph_payload,
        }

    def ingest_once(self, *, force_refresh: bool = False) -> int:
        return self.ingestion.ingest_once(force_refresh=force_refresh)

    def status(self) -> Dict[str, Any]:
        base = self.ingestion.status()
        base.update({
            "semantic_index_size": self.store.size,
            "semantic_index_dim": self.store.dimension,
        })
        return base


__all__ = ["BSMRAGOrchestrator", "SearchResult"]
