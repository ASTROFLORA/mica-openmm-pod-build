"""Compatibility communication package layered into core, legacy reports, and observability.

Preferred imports for new code:
- ``bsm.communication.core`` for typed envelopes and bus primitives
- ``bsm.communication.legacy_reports`` for manuscript-era compatibility models
- ``bsm.communication.observability`` for telemetry and error-artifact adapters

The flat package surface is preserved for legacy callers.
"""

from .core import (
    AgentMessage,
    AgentPersona,
    Attachment,
    CommunicationProtocol,
    ErrorPayload,
    MessageBus,
    MessageHeader,
    MessageStore,
    MessageType,
    ProposalPayload,
    ResearchIntent,
    ResultPayload,
    ReviewDecision,
    SafetyTier,
    Topic,
    TopicRegistry,
    ValidationPayload,
)
from .legacy_reports import (
    Citation,
    ConsolidatedPaper,
    DiscussionSection,
    ExperimentMetadata,
    LabReport,
    MethodsSection,
    PeerFeedback,
    QualityScore,
    ReproducibilityData,
    ResultsSection,
)
from .observability import (
    ArtifactRecord,
    ErrorArtifactWriter,
    RuntimeErrorArtifactWriter,
    RuntimeTelemetryEmitter,
    TelemetryEmitter,
)

__all__ = [
    # Message schemas
    "AgentMessage",
    "AgentPersona",
    "MessageHeader",
    "MessageStore",
    "MessageType",
    "ResearchIntent",
    "ProposalPayload",
    "ResultPayload",
    "ValidationPayload",
    "ErrorPayload",
    "Attachment",
    "ReviewDecision",
    "SafetyTier",
    "Topic",
    "ErrorArtifactWriter",
    "ArtifactRecord",
    "RuntimeErrorArtifactWriter",
    "RuntimeTelemetryEmitter",
    "TelemetryEmitter",
    # Lab reports
    "LabReport",
    "ConsolidatedPaper",
    "ExperimentMetadata",
    "MethodsSection",
    "PeerFeedback",
    "QualityScore",
    "ResultsSection",
    "DiscussionSection",
    "Citation",
    "ReproducibilityData",
    # Protocol
    "CommunicationProtocol",
    "MessageBus",
    "TopicRegistry",
]

__version__ = "1.0.0"
