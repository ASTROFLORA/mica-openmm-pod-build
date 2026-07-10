"""
BSM Events Module
=================

Sistema de Event Sourcing para:
- Tracking de citas y referencias
- Auditoría de consultas
- Analytics de búsqueda
- Replay de estados

Author: BSM Modernization Initiative
"""

from .citation_events import (
    EventType,
    EventPriority,
    BaseEvent,
    CitationEvent,
    SearchEvent,
    EventStore,
    InMemoryEventStore,
    FileEventStore,
    EventBus,
    CitationState,
    CitationAggregate,
    SearchAnalytics,
    create_event_system,
)

__all__ = [
    "EventType",
    "EventPriority",
    "BaseEvent",
    "CitationEvent",
    "SearchEvent",
    "EventStore",
    "InMemoryEventStore",
    "FileEventStore",
    "EventBus",
    "CitationState",
    "CitationAggregate",
    "SearchAnalytics",
    "create_event_system",
]
