"""
BSM Search Module
=================

Motor de búsqueda híbrido que integra múltiples fuentes:
- Milvus 5-vectores (ProtT5, ESM-C, BioLinkBERT, SciBERT, node2vec)
- Neo4j GraphRAG
- BLAST alignment
- BM25 text search
- RRF fusion

Author: BSM Modernization Initiative
"""

from .hybrid_search_engine import (
    SearchStrategy,
    QueryIntent,
    SearchConfig,
    SourceResult,
    UnifiedResult,
    SearchResponse,
    SearchSource,
    MilvusVectorSource,
    Neo4jGraphSource,
    BlastAlignmentSource,
    BM25TextSource,
    IntentDetector,
    HybridSearchEngine,
    create_hybrid_search_engine,
)

__all__ = [
    "SearchStrategy",
    "QueryIntent",
    "SearchConfig",
    "SourceResult",
    "UnifiedResult",
    "SearchResponse",
    "SearchSource",
    "MilvusVectorSource",
    "Neo4jGraphSource",
    "BlastAlignmentSource",
    "BM25TextSource",
    "IntentDetector",
    "HybridSearchEngine",
    "create_hybrid_search_engine",
]
