"""Unified Retrieval-Augmented Generation facade for BSM-BUDO."""
from .artifact_ingestion import (
    ArtifactRecord,
    BSMArtifactExtractor,
    BSMArtifactIngestionService,
    build_document_text,
)
from .context_assembly import assemble_context
from .graph_bridge import GraphRAGBridge
from .orchestrator import BSMRAGOrchestrator, SearchResult
from .semantic_store import BSMSemanticStore, BSMDocument, BSMSemanticWeights, GLOBAL_SEMANTIC_INDEX

__all__ = [
    "ArtifactRecord",
    "BSMArtifactExtractor",
    "BSMArtifactIngestionService",
    "build_document_text",
    "assemble_context",
    "GraphRAGBridge",
    "BSMRAGOrchestrator",
    "SearchResult",
    "BSMSemanticStore",
    "BSMDocument",
    "BSMSemanticWeights",
    "GLOBAL_SEMANTIC_INDEX",
]
