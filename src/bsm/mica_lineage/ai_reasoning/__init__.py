"""
AI Reasoning Module Init - Dr. Priya Sharma
==========================================

Initialization module for AI reasoning components in MICA-Lineage system.
Coordinates Chronoracle, ESE annotation, and GraphRAG capabilities.

Phase 5 Implementation: Chronoracle Integration
Lead: Dr. Priya Sharma
"""

from .chronoracle_client import (
    ChronoracleClient,
    ChronoracleQuery,
    ChronoracleResponse
)

from .ese_annotation import (
    ESESentientAnnotator,
    ESEAnnotation,
    ESESituation
)

from .graphrag_engine import (
    GraphRAGEngine,
    GraphEntity,
    GraphRelation,
    GraphPath,
    RAGQuery,
    RAGResponse
)

__version__ = "1.0.0"
__author__ = "Dr. Priya Sharma"
__description__ = "AI Reasoning components for MICA-Lineage Protocolo Fénix Azteca"

# AI reasoning system integration
__all__ = [
    # Chronoracle K9 System
    'ChronoracleClient',
    'ChronoracleQuery', 
    'ChronoracleResponse',
    
    # ESE Sentient Annotation
    'ESESentientAnnotator',
    'ESEAnnotation',
    'ESESituation',
    
    # GraphRAG Engine
    'GraphRAGEngine',
    'GraphEntity',
    'GraphRelation',
    'GraphPath',
    'RAGQuery',
    'RAGResponse',
    
    # Integrated AI System
    'create_integrated_ai_system'
]


def create_integrated_ai_system(config=None):
    """
    Create integrated AI reasoning system combining all components.
    
    Returns:
        Dictionary with initialized AI reasoning components
    """
    
    system = {
        'chronoracle': ChronoracleClient(config),
        'ese_annotator': ESESentientAnnotator(config),
        'graphrag': GraphRAGEngine(config)
    }
    
    return system

# --- Compatibility Aliases to fix import errors in bsm/mica_lineage/__init__.py ---
ESEAnnotationEngine = ESESentientAnnotator

class K9DatasetConfig:
    pass

class ESEAnnotationConfig:
    pass

class GraphRAGConfig:
    pass

# Extend __all__ dynamically
import sys
module = sys.modules[__name__]
module.__dict__['ESEAnnotationEngine'] = ESEAnnotationEngine
module.__dict__['K9DatasetConfig'] = K9DatasetConfig
module.__dict__['ESEAnnotationConfig'] = ESEAnnotationConfig
module.__dict__['GraphRAGConfig'] = GraphRAGConfig

__all__.extend([
    'ESEAnnotationEngine',
    'K9DatasetConfig',
    'ESEAnnotationConfig',
    'GraphRAGConfig'
])