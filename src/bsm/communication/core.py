"""Stable communication-core exports for runtime compatibility layers."""

from .message_schema import (
    AgentMessage,
    AgentPersona,
    Attachment,
    ErrorPayload,
    MessageHeader,
    MessageType,
    ProposalPayload,
    ResearchIntent,
    ResultPayload,
    ReviewDecision,
    SafetyTier,
    ValidationPayload,
)
from .protocol import CommunicationProtocol, MessageBus, MessageStore, Topic, TopicRegistry

__all__ = [
    "AgentMessage",
    "AgentPersona",
    "Attachment",
    "CommunicationProtocol",
    "ErrorPayload",
    "MessageBus",
    "MessageHeader",
    "MessageStore",
    "MessageType",
    "ProposalPayload",
    "ResearchIntent",
    "ResultPayload",
    "ReviewDecision",
    "SafetyTier",
    "Topic",
    "TopicRegistry",
    "ValidationPayload",
]