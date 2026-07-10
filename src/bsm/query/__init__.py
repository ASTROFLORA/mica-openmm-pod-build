"""
Multi-Backend Query Engine

Unified query interface across multiple backends:
- Neo4j: Graph relationships, pathways, functional states
- Zilliz/Milvus: Vector similarity, embeddings (1280D)
- JSON-LD: Semantic reasoning, external KG integration

Created: October 8, 2025
Author: Alex Rodriguez
"""

from .multi_backend_engine import MultiBackendQueryEngine

__all__ = ["MultiBackendQueryEngine"]
