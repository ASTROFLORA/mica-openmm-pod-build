"""
ESE (Ensemble Spectral Embedding) Module
=========================================

Phase 3: ESE signature extraction from MD trajectories.

Author: Alex Rodriguez (Architecture)
Contributors: Yuan Cheng (Algorithms), Aris Thorne (mdCATH/BioSites)
Date: October 8, 2025
"""

from .extractor import ESEExtractor, ESEConfig, MUDOPackager

__all__ = ["ESEExtractor", "ESEConfig", "MUDOPackager"]
