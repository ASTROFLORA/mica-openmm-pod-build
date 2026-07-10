"""
MICA-Lineage Main Module - Complete E2E Integration
================================================

MICA-Lineage (Protocolo Fénix Azteca) - Complete End-to-End System Integration.
Revolutionary protein taxonomic classification with multi-modal embeddings and AI reasoning.

BSM-BUDO-CEA Unified Implementation
Phases 1-6 Complete Integration

Team Implementation:
- Dr. Yuan Chen: Multi-modal embeddings (Phase 3-4)  
- Dr. Priya Sharma: AI reasoning (Phase 5)
- Alex Rodriguez: System architecture (Phase 1-2)
- Dr. Sofia Petrov: Infrastructure (Phase 0, 6)
- Dr. Aris Thorne: BioSites integration (Phase 3.6)
"""

import logging
import asyncio
from typing import Dict, List, Optional, Union, Any, Tuple
import numpy as np
from pathlib import Path
import time

# Import AI Reasoning components (Dr. Priya Sharma)
from .ai_reasoning import (
    ChronoracleClient,
    ESEAnnotationEngine, 
    GraphRAGEngine,
    K9DatasetConfig,
    ESEAnnotationConfig,
    GraphRAGConfig
)

# Import Embeddings components (Dr. Yuan Chen)  
from .embeddings import (
    UnifiedEmbeddingPipeline,
    EmbeddingPipelineConfig,
    PipelineOutput,
    MUDO,
    MUDOPackagingSystem
)
from .compatibility_checker import (
    MICALineageCompatibilityChecker,
    CompatibilityResult,
    SystemRequirements
)

logger = logging.getLogger(__name__)

# Module version
__version__ = "1.0.0"

# Export main classes
__all__ = [
    'MICALineageSystem',
    'MICALineageConfig', 
    'MICALineageOutput',
    'ProtocoloFenixAzteca',
    'TaxonomicClassificationEngine',
    'MICALineageCompatibilityChecker',
    'CompatibilityResult',
    'SystemRequirements'
]