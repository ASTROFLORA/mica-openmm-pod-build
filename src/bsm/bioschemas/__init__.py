"""
BioSchemas JSON-LD Transformation Module

This module provides transformers to convert BUDO objects into 
BioSchemas-compliant JSON-LD following Schema.org vocabularies.

Supported Profiles:
- Protein (https://bioschemas.org/profiles/Protein/0.11-RELEASE)
- BioChemEntity
- Gene

Created: October 8, 2025
Author: Alex Rodriguez (AI Systems Architecture)
"""

from .transformer import BioSchemasTransformer

__all__ = ["BioSchemasTransformer"]
