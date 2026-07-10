"""
BSM Event Sourcing System
==========================

Sistema de event sourcing para:
- Tracking de citas y referencias
- Auditoría de consultas
- Replay de estados
- Integración con ATOM (versión simplificada)

Basado en:
- CITATION_DRIVEN_RAG_ARCHITECTURE.md
- MICA_RAG_V2_IMPLEMENTATION_PLAN.md

Author: BSM Modernization Initiative
Version: 3.0.0
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, TypeVar, Generic, Callable, Tuple
from enum import Enum, auto
from datetime import datetime, timezone
from abc import ABC, abstractmethod
import hashlib
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================================
# EVENT TYPES
# ============================================================================

class EventType(Enum):
    """Tipos de eventos del sistema"""
    
    # Citation Events
    CITATION_CREATED = "citation.created"
    CITATION_VALIDATED = "citation.validated"
    CITATION_INVALIDATED = "citation.invalidated"
    CITATION_UPDATED = "citation.updated"
    
    # Search Events
    SEARCH_EXECUTED = "search.executed"
    SEARCH_RESULTS_RETURNED = "search.results_returned"
    SEARCH_RESULT_CLICKED = "search.result_clicked"
    SEARCH_FEEDBACK_RECEIVED = "search.feedback_received"
    
    # Knowledge Events
    KNOWLEDGE_INDEXED = "knowledge.indexed"
    KNOWLEDGE_UPDATED = "knowledge.updated"
    KNOWLEDGE_DELETED = "knowledge.deleted"
    KNOWLEDGE_LINKED = "knowledge.linked"
    
    # Embedding Events
    EMBEDDING_GENERATED = "embedding.generated"
    EMBEDDING_CACHED = "embedding.cached"
    
    # User Events
    USER_QUERY = "user.query"
    USER_SESSION_START = "user.session_start"
    USER_SESSION_END = "user.session_end"
    
    # System Events
    SYSTEM_INITIALIZED = "system.initialized"
    SYSTEM_ERROR = "system.error"
    SYSTEM_CHECKPOINT = "system.checkpoint"


class EventPriority(Enum):
    """Prioridad del evento"""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


# ============================================================================
# BASE EVENT
# ============================================================================

@dataclass
class BaseEvent:
    """Evento base inmutable"""
    
    # Identificadores
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType = EventType.SYSTEM_INITIALIZED
    
    # Temporal
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Contexto
    aggregate_id: Optional[str] = None  # ID del agregado (ej: documento, usuario)
    correlation_id: Optional[str] = None  # Para trazar flujos
    causation_id: Optional[str] = None  # Evento que causó este
    
    # Payload
    data: Dict[str, Any] = field(default_factory=dict)
    
    # Metadata
    version: int = 1
    source: str = "bsm"
    priority: EventPriority = EventPriority.NORMAL
    
    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "aggregate_id": self.aggregate_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "data": self.data,
            "version": self.version,
            "source": self.source,
            "priority": self.priority.value
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseEvent":
        """Deserializa desde diccionario"""
        return cls(
            event_id=data["event_id"],
            event_type=EventType(data["event_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            aggregate_id=data.get("aggregate_id"),
            correlation_id=data.get("correlation_id"),
            causation_id=data.get("causation_id"),
            data=data.get("data", {}),
            version=data.get("version", 1),
            source=data.get("source", "bsm"),
            priority=EventPriority(data.get("priority", 1))
        )
    
    def to_json(self) -> str:
        """Serializa a JSON"""
        return json.dumps(self.to_dict(), default=str)
    
    @classmethod
    def from_json(cls, json_str: str) -> "BaseEvent":
        """Deserializa desde JSON"""
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# CITATION EVENTS
# ============================================================================

@dataclass
class CitationEvent(BaseEvent):
    """Evento específico de citas"""
    
    # Campos específicos de citation (en data)
    # data = {
    #     "citation_id": str,
    #     "doi": str,
    #     "title": str,
    #     "authors": List[str],
    #     "year": int,
    #     "journal": str,
    #     "apa_formatted": str,
    #     "validation_score": float,
    #     "source_document_id": str
    # }
    
    @classmethod
    def create_citation(
        cls,
        citation_id: str,
        doi: Optional[str],
        title: str,
        authors: List[str],
        year: int,
        journal: Optional[str] = None,
        source_document_id: Optional[str] = None,
        correlation_id: Optional[str] = None
    ) -> "CitationEvent":
        """Crea evento de nueva cita"""
        apa = cls._format_apa(authors, year, title, journal)
        
        return cls(
            event_type=EventType.CITATION_CREATED,
            aggregate_id=citation_id,
            correlation_id=correlation_id,
            data={
                "citation_id": citation_id,
                "doi": doi,
                "title": title,
                "authors": authors,
                "year": year,
                "journal": journal,
                "apa_formatted": apa,
                "validation_score": 0.0,
                "source_document_id": source_document_id
            }
        )
    
    @classmethod
    def validate_citation(
        cls,
        citation_id: str,
        validation_score: float,
        validated_doi: Optional[str] = None,
        validator: str = "crossref",
        causation_id: Optional[str] = None
    ) -> "CitationEvent":
        """Crea evento de validación de cita"""
        return cls(
            event_type=EventType.CITATION_VALIDATED,
            aggregate_id=citation_id,
            causation_id=causation_id,
            data={
                "citation_id": citation_id,
                "validation_score": validation_score,
                "validated_doi": validated_doi,
                "validator": validator,
                "validated_at": datetime.now(timezone.utc).isoformat()
            },
            priority=EventPriority.HIGH
        )
    
    @staticmethod
    def _format_apa(
        authors: List[str],
        year: int,
        title: str,
        journal: Optional[str]
    ) -> str:
        """Formatea cita en estilo APA"""
        if not authors:
            author_str = "Unknown"
        elif len(authors) == 1:
            author_str = authors[0]
        elif len(authors) == 2:
            author_str = f"{authors[0]} & {authors[1]}"
        else:
            author_str = f"{authors[0]} et al."
        
        base = f"{author_str} ({year}). {title}"
        
        if journal:
            return f"{base}. {journal}."
        return f"{base}."


@dataclass
class SearchEvent(BaseEvent):
    """Evento específico de búsqueda"""
    
    @classmethod
    def search_executed(
        cls,
        query: str,
        strategy: str,
        sources_used: List[str],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> "SearchEvent":
        """Crea evento de búsqueda ejecutada"""
        return cls(
            event_type=EventType.SEARCH_EXECUTED,
            aggregate_id=session_id,
            data={
                "query": query,
                "query_hash": hashlib.sha256(query.encode()).hexdigest()[:16],
                "strategy": strategy,
                "sources_used": sources_used,
                "user_id": user_id,
                "session_id": session_id
            }
        )
    
    @classmethod
    def results_returned(
        cls,
        search_event_id: str,
        result_count: int,
        top_results: List[str],
        search_time_ms: float
    ) -> "SearchEvent":
        """Crea evento de resultados retornados"""
        return cls(
            event_type=EventType.SEARCH_RESULTS_RETURNED,
            causation_id=search_event_id,
            data={
                "result_count": result_count,
                "top_results": top_results[:10],  # Solo top 10
                "search_time_ms": search_time_ms
            }
        )
    
    @classmethod
    def result_clicked(
        cls,
        search_event_id: str,
        result_id: str,
        result_rank: int,
        user_id: Optional[str] = None
    ) -> "SearchEvent":
        """Crea evento de click en resultado"""
        return cls(
            event_type=EventType.SEARCH_RESULT_CLICKED,
            causation_id=search_event_id,
            aggregate_id=result_id,
            data={
                "result_id": result_id,
                "result_rank": result_rank,
                "user_id": user_id
            },
            priority=EventPriority.HIGH  # Importante para feedback
        )


# ============================================================================
# EVENT STORE INTERFACE
# ============================================================================

class EventStore(ABC):
    """Interfaz para almacenar eventos"""
    
    @abstractmethod
    async def append(self, event: BaseEvent) -> str:
        """Agrega evento al store"""
        pass
    
    @abstractmethod
    async def get_by_id(self, event_id: str) -> Optional[BaseEvent]:
        """Obtiene evento por ID"""
        pass
    
    @abstractmethod
    async def get_by_aggregate(
        self,
        aggregate_id: str,
        event_types: Optional[List[EventType]] = None
    ) -> List[BaseEvent]:
        """Obtiene eventos por aggregate ID"""
        pass
    
    @abstractmethod
    async def get_by_correlation(self, correlation_id: str) -> List[BaseEvent]:
        """Obtiene eventos por correlation ID"""
        pass
    
    @abstractmethod
    async def get_range(
        self,
        start_time: datetime,
        end_time: datetime,
        event_types: Optional[List[EventType]] = None
    ) -> List[BaseEvent]:
        """Obtiene eventos en rango temporal"""
        pass


# ============================================================================
# IN-MEMORY EVENT STORE
# ============================================================================

class InMemoryEventStore(EventStore):
    """Almacén de eventos en memoria (desarrollo/testing)"""
    
    def __init__(self, max_events: int = 100000):
        self._events: List[BaseEvent] = []
        self._index_by_id: Dict[str, int] = {}
        self._index_by_aggregate: Dict[str, List[int]] = {}
        self._index_by_correlation: Dict[str, List[int]] = {}
        self._max_events = max_events
        self._lock = asyncio.Lock()
    
    async def append(self, event: BaseEvent) -> str:
        """Agrega evento"""
        async with self._lock:
            # Límite de eventos
            if len(self._events) >= self._max_events:
                # Eliminar 10% más antiguos
                cutoff = self._max_events // 10
                self._events = self._events[cutoff:]
                self._rebuild_indices()
            
            idx = len(self._events)
            self._events.append(event)
            
            # Indexar
            self._index_by_id[event.event_id] = idx
            
            if event.aggregate_id:
                if event.aggregate_id not in self._index_by_aggregate:
                    self._index_by_aggregate[event.aggregate_id] = []
                self._index_by_aggregate[event.aggregate_id].append(idx)
            
            if event.correlation_id:
                if event.correlation_id not in self._index_by_correlation:
                    self._index_by_correlation[event.correlation_id] = []
                self._index_by_correlation[event.correlation_id].append(idx)
            
            logger.debug(f"Event appended: {event.event_type.value} ({event.event_id})")
            return event.event_id
    
    async def get_by_id(self, event_id: str) -> Optional[BaseEvent]:
        """Obtiene por ID"""
        idx = self._index_by_id.get(event_id)
        if idx is not None:
            return self._events[idx]
        return None
    
    async def get_by_aggregate(
        self,
        aggregate_id: str,
        event_types: Optional[List[EventType]] = None
    ) -> List[BaseEvent]:
        """Obtiene por aggregate"""
        indices = self._index_by_aggregate.get(aggregate_id, [])
        events = [self._events[i] for i in indices]
        
        if event_types:
            events = [e for e in events if e.event_type in event_types]
        
        return sorted(events, key=lambda e: e.timestamp)
    
    async def get_by_correlation(self, correlation_id: str) -> List[BaseEvent]:
        """Obtiene por correlation"""
        indices = self._index_by_correlation.get(correlation_id, [])
        events = [self._events[i] for i in indices]
        return sorted(events, key=lambda e: e.timestamp)
    
    async def get_range(
        self,
        start_time: datetime,
        end_time: datetime,
        event_types: Optional[List[EventType]] = None
    ) -> List[BaseEvent]:
        """Obtiene en rango temporal"""
        events = [
            e for e in self._events
            if start_time <= e.timestamp <= end_time
        ]
        
        if event_types:
            events = [e for e in events if e.event_type in event_types]
        
        return sorted(events, key=lambda e: e.timestamp)
    
    def _rebuild_indices(self):
        """Reconstruye índices"""
        self._index_by_id.clear()
        self._index_by_aggregate.clear()
        self._index_by_correlation.clear()
        
        for idx, event in enumerate(self._events):
            self._index_by_id[event.event_id] = idx
            
            if event.aggregate_id:
                if event.aggregate_id not in self._index_by_aggregate:
                    self._index_by_aggregate[event.aggregate_id] = []
                self._index_by_aggregate[event.aggregate_id].append(idx)
            
            if event.correlation_id:
                if event.correlation_id not in self._index_by_correlation:
                    self._index_by_correlation[event.correlation_id] = []
                self._index_by_correlation[event.correlation_id].append(idx)


# ============================================================================
# FILE-BASED EVENT STORE
# ============================================================================

class FileEventStore(EventStore):
    """Almacén de eventos basado en archivos (persistente)"""
    
    def __init__(self, base_path: str = "./data/events"):
        self._base_path = Path(base_path)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._current_file: Optional[Path] = None
        self._lock = asyncio.Lock()
    
    def _get_file_path(self, timestamp: datetime) -> Path:
        """Obtiene path del archivo por fecha"""
        date_str = timestamp.strftime("%Y-%m-%d")
        return self._base_path / f"events_{date_str}.jsonl"
    
    async def append(self, event: BaseEvent) -> str:
        """Agrega evento a archivo"""
        async with self._lock:
            file_path = self._get_file_path(event.timestamp)
            
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")
            
            logger.debug(f"Event persisted: {event.event_type.value}")
            return event.event_id
    
    async def get_by_id(self, event_id: str) -> Optional[BaseEvent]:
        """Busca evento por ID en todos los archivos"""
        for file_path in sorted(self._base_path.glob("events_*.jsonl"), reverse=True):
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    event = BaseEvent.from_json(line.strip())
                    if event.event_id == event_id:
                        return event
        return None
    
    async def get_by_aggregate(
        self,
        aggregate_id: str,
        event_types: Optional[List[EventType]] = None
    ) -> List[BaseEvent]:
        """Busca eventos por aggregate"""
        events = []
        
        for file_path in sorted(self._base_path.glob("events_*.jsonl")):
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    event = BaseEvent.from_json(line.strip())
                    if event.aggregate_id == aggregate_id:
                        if not event_types or event.event_type in event_types:
                            events.append(event)
        
        return sorted(events, key=lambda e: e.timestamp)
    
    async def get_by_correlation(self, correlation_id: str) -> List[BaseEvent]:
        """Busca por correlation"""
        events = []
        
        for file_path in sorted(self._base_path.glob("events_*.jsonl")):
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    event = BaseEvent.from_json(line.strip())
                    if event.correlation_id == correlation_id:
                        events.append(event)
        
        return sorted(events, key=lambda e: e.timestamp)
    
    async def get_range(
        self,
        start_time: datetime,
        end_time: datetime,
        event_types: Optional[List[EventType]] = None
    ) -> List[BaseEvent]:
        """Obtiene eventos en rango"""
        events = []
        
        # Determinar archivos relevantes
        current = start_time
        while current <= end_time:
            file_path = self._get_file_path(current)
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        event = BaseEvent.from_json(line.strip())
                        if start_time <= event.timestamp <= end_time:
                            if not event_types or event.event_type in event_types:
                                events.append(event)
            current = datetime(current.year, current.month, current.day) + \
                     __import__("datetime").timedelta(days=1)
        
        return sorted(events, key=lambda e: e.timestamp)


# ============================================================================
# EVENT BUS
# ============================================================================

EventHandler = Callable[[BaseEvent], None]


class EventBus:
    """Bus de eventos con suscripciones"""
    
    def __init__(self, event_store: Optional[EventStore] = None):
        self._store = event_store or InMemoryEventStore()
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._global_handlers: List[EventHandler] = []
    
    def subscribe(
        self,
        event_type: EventType,
        handler: EventHandler
    ) -> None:
        """Suscribe handler a tipo de evento"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug(f"Handler subscribed to {event_type.value}")
    
    def subscribe_all(self, handler: EventHandler) -> None:
        """Suscribe handler a todos los eventos"""
        self._global_handlers.append(handler)
    
    async def publish(self, event: BaseEvent) -> str:
        """Publica evento"""
        # Persistir
        event_id = await self._store.append(event)
        
        # Notificar handlers específicos
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Handler error for {event.event_type.value}: {e}")
        
        # Notificar handlers globales
        for handler in self._global_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Global handler error: {e}")
        
        return event_id
    
    async def replay(
        self,
        aggregate_id: str,
        handler: EventHandler
    ) -> None:
        """Replay de eventos para un aggregate"""
        events = await self._store.get_by_aggregate(aggregate_id)
        
        for event in events:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Replay error: {e}")


# ============================================================================
# CITATION AGGREGATE
# ============================================================================

@dataclass
class CitationState:
    """Estado actual de una cita (construido desde eventos)"""
    citation_id: str
    doi: Optional[str] = None
    title: str = ""
    authors: List[str] = field(default_factory=list)
    year: int = 0
    journal: Optional[str] = None
    apa_formatted: str = ""
    validation_score: float = 0.0
    is_validated: bool = False
    created_at: Optional[datetime] = None
    validated_at: Optional[datetime] = None
    version: int = 0


class CitationAggregate:
    """Aggregate para citas con event sourcing"""
    
    def __init__(self, event_bus: EventBus):
        self._bus = event_bus
        self._state_cache: Dict[str, CitationState] = {}
    
    async def create_citation(
        self,
        doi: Optional[str],
        title: str,
        authors: List[str],
        year: int,
        journal: Optional[str] = None,
        source_document_id: Optional[str] = None,
        correlation_id: Optional[str] = None
    ) -> str:
        """Crea nueva cita"""
        citation_id = str(uuid.uuid4())
        
        event = CitationEvent.create_citation(
            citation_id=citation_id,
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            source_document_id=source_document_id,
            correlation_id=correlation_id
        )
        
        await self._bus.publish(event)
        
        # Actualizar cache
        self._state_cache[citation_id] = CitationState(
            citation_id=citation_id,
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            apa_formatted=event.data["apa_formatted"],
            created_at=event.timestamp,
            version=1
        )
        
        return citation_id
    
    async def validate_citation(
        self,
        citation_id: str,
        validation_score: float,
        validated_doi: Optional[str] = None,
        validator: str = "crossref"
    ) -> None:
        """Valida cita existente"""
        # Obtener evento de creación
        creation_event = await self._get_creation_event(citation_id)
        if not creation_event:
            raise ValueError(f"Citation not found: {citation_id}")
        
        event = CitationEvent.validate_citation(
            citation_id=citation_id,
            validation_score=validation_score,
            validated_doi=validated_doi,
            validator=validator,
            causation_id=creation_event.event_id
        )
        
        await self._bus.publish(event)
        
        # Actualizar cache
        if citation_id in self._state_cache:
            state = self._state_cache[citation_id]
            state.validation_score = validation_score
            state.is_validated = validation_score >= 0.8
            state.validated_at = event.timestamp
            state.version += 1
            if validated_doi:
                state.doi = validated_doi
    
    async def get_state(self, citation_id: str) -> Optional[CitationState]:
        """Obtiene estado actual de cita"""
        # Verificar cache
        if citation_id in self._state_cache:
            return self._state_cache[citation_id]
        
        # Reconstruir desde eventos
        events = await self._bus._store.get_by_aggregate(
            citation_id,
            [EventType.CITATION_CREATED, EventType.CITATION_VALIDATED]
        )
        
        if not events:
            return None
        
        state = self._rebuild_state(events)
        self._state_cache[citation_id] = state
        return state
    
    async def _get_creation_event(self, citation_id: str) -> Optional[CitationEvent]:
        """Obtiene evento de creación"""
        events = await self._bus._store.get_by_aggregate(
            citation_id,
            [EventType.CITATION_CREATED]
        )
        return events[0] if events else None
    
    def _rebuild_state(self, events: List[BaseEvent]) -> CitationState:
        """Reconstruye estado desde eventos"""
        state = CitationState(citation_id="")
        
        for event in events:
            if event.event_type == EventType.CITATION_CREATED:
                state.citation_id = event.data["citation_id"]
                state.doi = event.data.get("doi")
                state.title = event.data["title"]
                state.authors = event.data["authors"]
                state.year = event.data["year"]
                state.journal = event.data.get("journal")
                state.apa_formatted = event.data["apa_formatted"]
                state.created_at = event.timestamp
                state.version = 1
            
            elif event.event_type == EventType.CITATION_VALIDATED:
                state.validation_score = event.data["validation_score"]
                state.is_validated = event.data["validation_score"] >= 0.8
                state.validated_at = event.timestamp
                if event.data.get("validated_doi"):
                    state.doi = event.data["validated_doi"]
                state.version += 1
        
        return state


# ============================================================================
# SEARCH ANALYTICS
# ============================================================================

class SearchAnalytics:
    """Análisis de búsquedas usando eventos"""
    
    def __init__(self, event_bus: EventBus):
        self._bus = event_bus
        self._query_counts: Dict[str, int] = {}
        self._click_through: Dict[str, List[int]] = {}  # query_hash -> ranks clicked
        
        # Suscribirse a eventos
        self._bus.subscribe(EventType.SEARCH_EXECUTED, self._on_search)
        self._bus.subscribe(EventType.SEARCH_RESULT_CLICKED, self._on_click)
    
    async def log_search(
        self,
        query: str,
        strategy: str,
        sources: List[str],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> str:
        """Registra búsqueda"""
        event = SearchEvent.search_executed(
            query=query,
            strategy=strategy,
            sources_used=sources,
            user_id=user_id,
            session_id=session_id
        )
        return await self._bus.publish(event)
    
    async def log_results(
        self,
        search_event_id: str,
        result_count: int,
        top_results: List[str],
        search_time_ms: float
    ) -> str:
        """Registra resultados"""
        event = SearchEvent.results_returned(
            search_event_id=search_event_id,
            result_count=result_count,
            top_results=top_results,
            search_time_ms=search_time_ms
        )
        return await self._bus.publish(event)
    
    async def log_click(
        self,
        search_event_id: str,
        result_id: str,
        result_rank: int,
        user_id: Optional[str] = None
    ) -> str:
        """Registra click"""
        event = SearchEvent.result_clicked(
            search_event_id=search_event_id,
            result_id=result_id,
            result_rank=result_rank,
            user_id=user_id
        )
        return await self._bus.publish(event)
    
    def _on_search(self, event: SearchEvent):
        """Handler para búsquedas"""
        query_hash = event.data.get("query_hash", "")
        self._query_counts[query_hash] = self._query_counts.get(query_hash, 0) + 1
    
    def _on_click(self, event: SearchEvent):
        """Handler para clicks"""
        # Usar causation_id para vincular con búsqueda original
        search_id = event.causation_id
        rank = event.data.get("result_rank", 0)
        
        if search_id:
            if search_id not in self._click_through:
                self._click_through[search_id] = []
            self._click_through[search_id].append(rank)
    
    def get_popular_queries(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Obtiene queries más populares"""
        sorted_queries = sorted(
            self._query_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return sorted_queries[:limit]
    
    def get_click_through_rate(self) -> float:
        """Calcula CTR general"""
        total_searches = sum(self._query_counts.values())
        total_clicks = sum(len(clicks) for clicks in self._click_through.values())
        
        if total_searches == 0:
            return 0.0
        return total_clicks / total_searches


# ============================================================================
# FACTORY
# ============================================================================

async def create_event_system(
    persistent: bool = True,
    base_path: str = "./data/events"
) -> Tuple[EventBus, CitationAggregate, SearchAnalytics]:
    """
    Factory para crear sistema de eventos completo.
    
    Args:
        persistent: Si True, usa almacenamiento en archivo
        base_path: Ruta base para eventos persistentes
        
    Returns:
        Tuple de (EventBus, CitationAggregate, SearchAnalytics)
    """
    if persistent:
        store = FileEventStore(base_path)
    else:
        store = InMemoryEventStore()
    
    bus = EventBus(store)
    citations = CitationAggregate(bus)
    analytics = SearchAnalytics(bus)
    
    logger.info("✅ Event system initialized")
    return bus, citations, analytics


# ============================================================================
# EXPORTS
# ============================================================================

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
