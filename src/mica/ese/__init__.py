"""
mica.ese — Public surface for SLICE P3 ESE Wrapper.

Usage:
    from mica.ese import normalize_and_register, EseNormalizeResult

This module is the canonical entry-point for all ESE artifact production.
"""
from .contracts import (
    EseArtifact,
    EseGraphLiteEdge,
    EseGraphLiteNode,
    EseGraphLitePayload,
    EseGraphLiteSummary,
    EseLitePayload,
    EseLiteResidueFeature,
    EseNormalizeResult,
)
from .graph_lite import build_ese_graph_lite
from .wrapper import (
    EseArtifactGate,
    ESMDANCE_MODEL_REF,
    ESE_LITE_ARTIFACT_KIND,
    ESE_GRAPH_LITE_ARTIFACT_KIND,
    normalize_and_register,
)

__all__ = [
    # Core entry-point
    "normalize_and_register",
    # Contracts
    "EseArtifact",
    "EseLitePayload",
    "EseLiteResidueFeature",
    "EseGraphLitePayload",
    "EseGraphLiteNode",
    "EseGraphLiteEdge",
    "EseGraphLiteSummary",
    "EseNormalizeResult",
    # Builders
    "build_ese_graph_lite",
    # Gates
    "EseArtifactGate",
    # Constants
    "ESMDANCE_MODEL_REF",
    "ESE_LITE_ARTIFACT_KIND",
    "ESE_GRAPH_LITE_ARTIFACT_KIND",
]
