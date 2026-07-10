# MICA Cognitive Architecture Package
"""
🧬 MICA Cognitive Architecture - Advanced AI System

Implementación de la arquitectura cognitiva distribuida siguiendo
la Guía Técnica Española para sistemas de IA avanzados.

Componentes principales:
- BioBERT Scientific Information Pipeline
- Semantic Cache Manager con Redis
- Advanced Milvus Manager con indexación optimizada
- Multi-Agent Cognitive System
- Shared Metacognition Framework
"""

__version__ = "1.0.0"
__author__ = "MICA Development Team"
__description__ = "Advanced Cognitive Architecture for Scientific AI"

# Export main components
from .biobert_scientific_pipeline import create_biobert_pipeline
# DEPRECATED: semantic_cache_manager replaced by mica.infrastructure.redisvl_semantic_cache (2026-04-08)
# from .semantic_cache_manager import create_semantic_cache_manager
from .advanced_milvus_manager import create_advanced_milvus_manager
from .multi_agent_system import create_cognitive_architecture

__all__ = [
    "create_biobert_pipeline",
    # "create_semantic_cache_manager",  # DEPRECATED — use mica.infrastructure.redisvl_semantic_cache
    "create_advanced_milvus_manager",
    "create_cognitive_architecture"
]