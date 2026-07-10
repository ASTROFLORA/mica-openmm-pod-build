"""Canonical acquisition and query protocol contracts for the literature consolidation lane."""

from .fulltext_acquisition import (
    DegradationEntry,
    FullTextAcquisitionRequest,
    FullTextAcquisitionResult,
    PaperRef,
)
from .query_protocol import (
    LiteratureQueryResult,
    LiteratureQuerySpec,
    PROTOCOL_VERSION,
)
from .poll_envelope import normalize_poll_envelope
from .provider_compiler import (
    DegradationReason,
    DegradedProvider,
    LiteratureSource,
    ProviderCapability,
    ProviderCompiler,
    ProviderExecutionPlan,
)
from .query_facade import (
    LiteratureQueryFacadeRequest,
    LiteratureQueryFacadeResult,
    VALID_LANES,
)
from .iterative_intelligence import (
    IterativeIntelligenceDirective,
    IterativeLiteratureSession,
)

__all__ = [
    "DegradationEntry",
    "FullTextAcquisitionRequest",
    "FullTextAcquisitionResult",
    "PaperRef",
    "LiteratureQueryResult",
    "LiteratureQuerySpec",
    "PROTOCOL_VERSION",
    "DegradationReason",
    "DegradedProvider",
    "LiteratureSource",
    "ProviderCapability",
    "ProviderCompiler",
    "ProviderExecutionPlan",
    "normalize_poll_envelope",
    "LiteratureQueryFacadeRequest",
    "LiteratureQueryFacadeResult",
    "VALID_LANES",
    "IterativeIntelligenceDirective",
    "IterativeLiteratureSession",
]
