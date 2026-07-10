"""
BUDO Module
===========

BUDO V3 (Biological Unified Data Object) implementation.
Sentient, mutable protein entities with multi-modal embeddings.

Author: Alex Rodriguez
Date: October 8, 2025
"""

from .neo4j_service import BudoNeo4jService

__all__ = ["BudoNeo4jService"]
