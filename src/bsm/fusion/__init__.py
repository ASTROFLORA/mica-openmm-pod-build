#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BSM Fusion Module - Reciprocal Rank Fusion (RRF) Engine

Fusión inteligente de resultados de múltiples fuentes de retrieval.
"""

from .rrf_fusion import (
    RetrievalSource,
    RRFConfig,
    RankedResult,
    FusedResult,
    RRFFusionEngine,
    AdaptiveRRFEngine,
    create_rrf_engine,
    create_adaptive_rrf_engine,
)

__all__ = [
    "RetrievalSource",
    "RRFConfig",
    "RankedResult",
    "FusedResult",
    "RRFFusionEngine",
    "AdaptiveRRFEngine",
    "create_rrf_engine",
    "create_adaptive_rrf_engine",
]
