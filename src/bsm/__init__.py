#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM - BIOLOGICAL SEMANTIC MEMORY
Sistema completo de memoria semántica biológica con dual-core GraphRAG
"""

__version__ = "1.0.0"
__author__ = "Dr. Yuan Cheng"
__description__ = "Biological Semantic Memory System with Neo4j + Milvus dual-core architecture"

# === IMPORTS PRINCIPALES ===

from .config import (
    BSMConfig,
    BSMConfigManager,
    get_bsm_config,
    get_config_manager,
    load_bsm_config
)

from .bioschemas_transformer import (
    BioSchemasTransformer,
    BSMBatchProcessor,
    BioSchemasConfig,
    create_bsm_transformer
)

from .neo4j_integration import (
    BSMNeo4jIntegration,
    ProteinNode,
    BioSchemasNode,
    RelationshipData,
    BSMGraphSchema,
    create_bsm_neo4j_integration
)

from .milvus_integration import (
    BSMMilvusIntegration,
    ProteinEmbedding,
    SimilarityResult,
    BSMMilvusSchema,
    create_bsm_milvus_integration
)

from .query_engine import (
    BSMQueryEngine,
    BSMQuery,
    BSMResult,
    QueryType,
    QueryPriority,
    NaturalLanguageInterpreter,
    create_bsm_query_engine
)

from .validation_suite import (
    BSMValidationSuite,
    ValidationResult,
    BenchmarkResult,
    BSMTestRunner
)

# === FUNCIONES DE UTILIDAD ===

async def initialize_bsm_system(config_path: str = None) -> dict:
    """
    Inicializa sistema BSM completo
    
    Args:
        config_path: Ruta al archivo de configuración (opcional)
        
    Returns:
        Dict con componentes inicializados
    """
    # Cargar configuración
    if config_path:
        config = load_bsm_config(config_path)
    else:
        config = get_bsm_config()
    
    # Inicializar componentes
    components = {}
    
    try:
        # Transformer
        components['transformer'] = await create_bsm_transformer()
        
        # Neo4j integration
        components['neo4j'] = await create_bsm_neo4j_integration(config)
        
        # Milvus integration  
        components['milvus'] = await create_bsm_milvus_integration(config)
        
        # Query engine
        components['query_engine'] = await create_bsm_query_engine(config)
        
        return {
            'status': 'success',
            'components': components,
            'config': config,
            'version': __version__
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e),
            'components': components
        }

def get_bsm_info() -> dict:
    """
    Obtiene información del sistema BSM
    
    Returns:
        Dict con información del sistema
    """
    return {
        'name': 'Biological Semantic Memory',
        'version': __version__,
        'author': __author__,
        'description': __description__,
        'components': [
            'BioSchemas Transformer',
            'Neo4j Graph Integration',
            'Milvus Vector Integration', 
            'Dual-Core Query Engine',
            'Validation Suite'
        ],
        'features': [
            'PubMedBERT → BioSchemas JSON-LD transformation',
            'Neo4j graph storage for explicit knowledge',
            'Milvus vector storage for semantic embeddings',
            'Natural language query processing',
            'Hybrid GraphRAG architecture',
            'Comprehensive validation and testing'
        ]
    }

# === EXPORTACIONES ===

__all__ = [
    # Configuración
    'BSMConfig',
    'BSMConfigManager', 
    'get_bsm_config',
    'get_config_manager',
    'load_bsm_config',
    
    # Transformer
    'BioSchemasTransformer',
    'BSMBatchProcessor',
    'BioSchemasConfig',
    'create_bsm_transformer',
    
    # Neo4j
    'BSMNeo4jIntegration',
    'ProteinNode',
    'BioSchemasNode', 
    'RelationshipData',
    'BSMGraphSchema',
    'create_bsm_neo4j_integration',
    
    # Milvus
    'BSMMilvusIntegration',
    'ProteinEmbedding',
    'SimilarityResult',
    'BSMMilvusSchema', 
    'create_bsm_milvus_integration',
    
    # Query Engine
    'BSMQueryEngine',
    'BSMQuery',
    'BSMResult',
    'QueryType',
    'QueryPriority',
    'NaturalLanguageInterpreter',
    'create_bsm_query_engine',
    
    # Validación
    'BSMValidationSuite',
    'ValidationResult',
    'BenchmarkResult',
    'BSMTestRunner',
    
    # Utilidades
    'initialize_bsm_system',
    'get_bsm_info'
]