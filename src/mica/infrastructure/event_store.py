"""
event_store.py - Infrastructure Event Store (Event Sourcing Pattern)

Implements immutable event logging for infrastructure operations following
the OpenHands-inspired architecture. This provides:

1. **State Reconstruction**: Rebuild exact state by replaying events
2. **Audit Trail**: Forensic accountability for every dollar spent
3. **Resilience**: Recover from crashes without zombie instances
4. **Determinism**: Reproducible execution history for regulatory compliance

OpenHands-Inspired Improvements (v2.0):
- EventStoreABC: Abstract base for multiple backends (file, database, remote)
- EventFilter: Dedicated filter object for extensible querying
- sequence_id: Sequential IDs for efficient range queries
- FileStore injection: Dependency injection for storage backend
- Page-based caching: Efficient handling of large event volumes

Event Types:
- JobRequested: Initial request received
- ProvisioningPlanSelected: Scorer chose a provider/offer
- ProvisioningAttempted: Instance creation started
- ProvisioningSucceeded: Instance is running
- ProvisioningFailed: Instance creation failed
- InstanceHealthCheck: Periodic status update
- CheckpointCreated: Simulation checkpoint saved
- InstancePreempted: Spot instance reclaimed
- InstanceTerminated: Instance destroyed
- CostIncurred: Billing event

Usage:
    # With default file store
    store = FileEventStore(storage_path="events.jsonl")
    
    # With filter object
    filter = EventFilter(event_types=[EventType.COST_INCURRED], job_id="abc")
    events = store.search_events(filter)
    
    # Record events (auto-assigns sequence_id)
    store.append(JobRequestedEvent(job_id="abc", spec={...}))
    
    # Get by sequence ID
    event = store.get_event(42)
    
    # Reconstruct state
    state = store.reconstruct_state()

Author: MICA Team
Date: December 2024
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol, Type, Union
import threading
import uuid


# ============================================================================
# Storage Backend Abstraction (OpenHands Pattern)
# ============================================================================

class FileStore(Protocol):
    """
    Protocol for file storage backends.
    
    Allows dependency injection of different storage mechanisms
    (local filesystem, GCS, S3, etc.) for testing and flexibility.
    """
    
    def read(self, path: str) -> str:
        """Read entire file contents."""
        ...
    
    def write(self, path: str, content: str) -> None:
        """Write content to file (overwrite)."""
        ...
    
    def append(self, path: str, content: str) -> None:
        """Append content to file."""
        ...
    
    def exists(self, path: str) -> bool:
        """Check if file exists."""
        ...
    
    def delete(self, path: str) -> None:
        """Delete file."""
        ...


class LocalFileStore:
    """Default local filesystem storage backend."""
    
    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    
    def write(self, path: str, content: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    
    def append(self, path: str, content: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    
    def exists(self, path: str) -> bool:
        return Path(path).exists()
    
    def delete(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)


# ============================================================================
# Event Filter (OpenHands Pattern)
# ============================================================================

@dataclass
class EventFilter:
    """
    Dedicated filter object for event queries.
    
    More extensible than individual parameters - can add new filter
    criteria without changing method signatures.
    
    Example:
        filter = EventFilter(
            event_types=[EventType.COST_INCURRED],
            job_id="job-123",
            since=datetime(2024, 12, 1),
            exclude_types=(InstanceHealthCheckEvent,)
        )
        events = store.search_events(filter)
    """
    event_types: Optional[List["EventType"]] = None
    job_id: Optional[str] = None
    instance_id: Optional[str] = None
    provider: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    exclude_types: Optional[tuple] = None  # Tuple of event classes to exclude
    min_sequence_id: Optional[int] = None
    max_sequence_id: Optional[int] = None
    limit: Optional[int] = None
    
    def matches(self, event: "InfrastructureEvent") -> bool:
        """Check if an event matches this filter."""
        if self.event_types and event.event_type not in self.event_types:
            return False
        
        if self.job_id and event.job_id != self.job_id:
            return False
        
        if self.instance_id and event.instance_id != self.instance_id:
            return False
        
        if self.provider and event.provider != self.provider:
            return False

        if self.user_id and getattr(event, "user_id", None) != self.user_id:
            return False

        if self.session_id and getattr(event, "session_id", None) != self.session_id:
            return False
        
        if self.since:
            event_time = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if event_time < self.since:
                return False
        
        if self.until:
            event_time = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if event_time > self.until:
                return False
        
        if self.exclude_types and isinstance(event, self.exclude_types):
            return False
        
        if self.min_sequence_id is not None and event.sequence_id < self.min_sequence_id:
            return False
        
        if self.max_sequence_id is not None and event.sequence_id > self.max_sequence_id:
            return False
        
        return True


class EventType(Enum):
    """Types of infrastructure events."""
    # Job lifecycle
    JOB_REQUESTED = "job_requested"
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"
    
    # Provisioning
    PROVISIONING_PLAN_SELECTED = "provisioning_plan_selected"
    PROVISIONING_ATTEMPTED = "provisioning_attempted"
    PROVISIONING_SUCCEEDED = "provisioning_succeeded"
    PROVISIONING_FAILED = "provisioning_failed"
    
    # Instance lifecycle
    INSTANCE_READY = "instance_ready"
    INSTANCE_HEALTH_CHECK = "instance_health_check"
    INSTANCE_PREEMPTED = "instance_preempted"
    INSTANCE_TERMINATED = "instance_terminated"
    
    # Checkpointing
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    
    # Cost tracking
    COST_INCURRED = "cost_incurred"
    COST_LIMIT_WARNING = "cost_limit_warning"
    COST_LIMIT_EXCEEDED = "cost_limit_exceeded"
    
    # Security
    SECURITY_CHECK_PASSED = "security_check_passed"
    SECURITY_CHECK_FAILED = "security_check_failed"
    SAGA_EVENT = "saga_event"


@dataclass
class InfrastructureEvent(ABC):
    """
    Base class for all infrastructure events.
    
    Events are immutable records of what happened. They form an
    append-only log that can be replayed to reconstruct system state.
    
    OpenHands-inspired additions:
    - sequence_id: Integer for efficient range queries and ordering
    - event_id: UUID for global uniqueness
    """
    # Sequential ID (assigned by EventStore on append)
    sequence_id: int = field(default=0)
    
    # Unique event identifier
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    event_type: EventType = field(init=False)
    
    # Correlation IDs for tracing
    job_id: Optional[str] = None
    instance_id: Optional[str] = None
    provider: Optional[str] = None

    # Attribution
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    bucket: Optional[str] = None
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        data = asdict(self)
        data["event_type"] = self.event_type.value
        return data
    
    def to_json(self) -> str:
        """Serialize event to JSON string."""
        return json.dumps(self.to_dict(), default=str)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InfrastructureEvent":
        """Deserialize event from dictionary."""
        event_type = EventType(data.pop("event_type"))
        event_class = EVENT_TYPE_MAP.get(event_type, GenericEvent)
        return event_class(**data)


# ============================================================================
# Concrete Event Classes
# ============================================================================

@dataclass
class JobRequestedEvent(InfrastructureEvent):
    """A new job was requested."""
    event_type: EventType = field(default=EventType.JOB_REQUESTED, init=False)
    
    worker_type: str = ""
    gpu_type: str = ""
    gpu_count: int = 1
    docker_image: str = ""
    max_price_per_hour: Optional[float] = None
    max_total_cost: Optional[float] = None
    priority: str = "normal"
    
    # Full spec for reconstruction
    spec_json: str = ""


@dataclass
class JobQueuedEvent(InfrastructureEvent):
    """Job was added to the queue."""
    event_type: EventType = field(default=EventType.JOB_QUEUED, init=False)
    
    queue_position: int = 0
    priority: str = "normal"


@dataclass
class ProvisioningPlanSelectedEvent(InfrastructureEvent):
    """Scorer selected a provisioning plan."""
    event_type: EventType = field(default=EventType.PROVISIONING_PLAN_SELECTED, init=False)
    
    selected_provider: str = ""
    selected_offer_id: str = ""
    price_per_hour: float = 0.0
    gpu_type: str = ""
    scorer_name: str = ""
    score: float = 0.0
    alternatives_considered: int = 0


@dataclass
class ProvisioningAttemptedEvent(InfrastructureEvent):
    """Instance provisioning was attempted."""
    event_type: EventType = field(default=EventType.PROVISIONING_ATTEMPTED, init=False)
    
    offer_id: str = ""
    docker_image: str = ""
    attempt_number: int = 1


@dataclass
class ProvisioningSucceededEvent(InfrastructureEvent):
    """Instance was successfully provisioned."""
    event_type: EventType = field(default=EventType.PROVISIONING_SUCCEEDED, init=False)
    
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    price_per_hour: float = 0.0
    gpu_type: str = ""
    gpu_count: int = 1
    provision_time_seconds: float = 0.0


@dataclass
class ProvisioningFailedEvent(InfrastructureEvent):
    """Instance provisioning failed."""
    event_type: EventType = field(default=EventType.PROVISIONING_FAILED, init=False)
    
    error_message: str = ""
    error_code: Optional[str] = None
    attempt_number: int = 1
    will_retry: bool = False


@dataclass
class InstanceReadyEvent(InfrastructureEvent):
    """Instance is ready to accept work."""
    event_type: EventType = field(default=EventType.INSTANCE_READY, init=False)
    
    ssh_host: str = ""
    ssh_port: int = 22
    jupyter_url: Optional[str] = None
    startup_time_seconds: float = 0.0


@dataclass
class InstanceHealthCheckEvent(InfrastructureEvent):
    """Periodic health check of running instance."""
    event_type: EventType = field(default=EventType.INSTANCE_HEALTH_CHECK, init=False)
    
    status: str = ""  # "healthy", "degraded", "unresponsive"
    gpu_utilization: Optional[float] = None
    memory_utilization: Optional[float] = None
    cost_so_far: float = 0.0


@dataclass
class InstancePreemptedEvent(InfrastructureEvent):
    """Spot instance was reclaimed by provider."""
    event_type: EventType = field(default=EventType.INSTANCE_PREEMPTED, init=False)
    
    warning_received: bool = False
    warning_seconds_before: Optional[float] = None
    last_checkpoint: Optional[str] = None
    cost_at_preemption: float = 0.0


@dataclass
class InstanceTerminatedEvent(InfrastructureEvent):
    """Instance was terminated."""
    event_type: EventType = field(default=EventType.INSTANCE_TERMINATED, init=False)
    
    reason: str = ""  # "completed", "failed", "cancelled", "cost_limit", "preempted"
    final_cost: float = 0.0
    runtime_seconds: float = 0.0


@dataclass
class CheckpointCreatedEvent(InfrastructureEvent):
    """Simulation checkpoint was saved."""
    event_type: EventType = field(default=EventType.CHECKPOINT_CREATED, init=False)
    
    checkpoint_path: str = ""
    checkpoint_size_bytes: int = 0
    simulation_step: Optional[int] = None
    simulation_time_ns: Optional[float] = None


@dataclass
class CheckpointRestoredEvent(InfrastructureEvent):
    """Simulation was restored from checkpoint."""
    event_type: EventType = field(default=EventType.CHECKPOINT_RESTORED, init=False)
    
    checkpoint_path: str = ""
    restored_step: Optional[int] = None
    restored_time_ns: Optional[float] = None


@dataclass
class CostIncurredEvent(InfrastructureEvent):
    """Cost was incurred."""
    event_type: EventType = field(default=EventType.COST_INCURRED, init=False)
    
    amount_usd: float = 0.0
    duration_hours: float = 0.0
    price_per_hour: float = 0.0
    cumulative_cost: float = 0.0


@dataclass
class CostLimitWarningEvent(InfrastructureEvent):
    """Approaching cost limit."""
    event_type: EventType = field(default=EventType.COST_LIMIT_WARNING, init=False)
    
    current_cost: float = 0.0
    limit: float = 0.0
    percent_used: float = 0.0


@dataclass
class CostLimitExceededEvent(InfrastructureEvent):
    """Cost limit was exceeded."""
    event_type: EventType = field(default=EventType.COST_LIMIT_EXCEEDED, init=False)
    
    final_cost: float = 0.0
    limit: float = 0.0
    action_taken: str = ""  # "terminated", "paused"


@dataclass
class SecurityCheckPassedEvent(InfrastructureEvent):
    """Security check passed."""
    event_type: EventType = field(default=EventType.SECURITY_CHECK_PASSED, init=False)
    
    checks_performed: List[str] = field(default_factory=list)


@dataclass
class SecurityCheckFailedEvent(InfrastructureEvent):
    """Security check failed."""
    event_type: EventType = field(default=EventType.SECURITY_CHECK_FAILED, init=False)
    
    failed_checks: List[str] = field(default_factory=list)
    reason: str = ""
    action_taken: str = ""  # "rejected", "queued_for_approval"


@dataclass
class JobCompletedEvent(InfrastructureEvent):
    """Job completed successfully."""
    event_type: EventType = field(default=EventType.JOB_COMPLETED, init=False)
    
    exit_code: int = 0
    output_path: Optional[str] = None
    final_cost: float = 0.0
    runtime_seconds: float = 0.0


@dataclass
class JobFailedEvent(InfrastructureEvent):
    """Job failed."""
    event_type: EventType = field(default=EventType.JOB_FAILED, init=False)
    
    error_message: str = ""
    exit_code: Optional[int] = None
    cost_incurred: float = 0.0


@dataclass
class JobCancelledEvent(InfrastructureEvent):
    """Job was cancelled by user."""
    event_type: EventType = field(default=EventType.JOB_CANCELLED, init=False)
    
    cancelled_by: str = ""
    reason: Optional[str] = None
    cost_incurred: float = 0.0


@dataclass
class GenericEvent(InfrastructureEvent):
    """Generic event for extensibility."""
    event_type: EventType = field(default=EventType.JOB_REQUESTED, init=False)
    
    custom_type: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SagaEvent(InfrastructureEvent):
    """Audit-friendly saga event for driver runs."""

    event_type: EventType = field(default=EventType.SAGA_EVENT, init=False)

    saga_session_id: str = ""
    run_id: Optional[str] = None
    stage: str = ""
    status: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


# Event type to class mapping
EVENT_TYPE_MAP: Dict[EventType, Type[InfrastructureEvent]] = {
    EventType.JOB_REQUESTED: JobRequestedEvent,
    EventType.JOB_QUEUED: JobQueuedEvent,
    EventType.JOB_COMPLETED: JobCompletedEvent,
    EventType.JOB_FAILED: JobFailedEvent,
    EventType.JOB_CANCELLED: JobCancelledEvent,
    EventType.PROVISIONING_PLAN_SELECTED: ProvisioningPlanSelectedEvent,
    EventType.PROVISIONING_ATTEMPTED: ProvisioningAttemptedEvent,
    EventType.PROVISIONING_SUCCEEDED: ProvisioningSucceededEvent,
    EventType.PROVISIONING_FAILED: ProvisioningFailedEvent,
    EventType.INSTANCE_READY: InstanceReadyEvent,
    EventType.INSTANCE_HEALTH_CHECK: InstanceHealthCheckEvent,
    EventType.INSTANCE_PREEMPTED: InstancePreemptedEvent,
    EventType.INSTANCE_TERMINATED: InstanceTerminatedEvent,
    EventType.CHECKPOINT_CREATED: CheckpointCreatedEvent,
    EventType.CHECKPOINT_RESTORED: CheckpointRestoredEvent,
    EventType.COST_INCURRED: CostIncurredEvent,
    EventType.COST_LIMIT_WARNING: CostLimitWarningEvent,
    EventType.COST_LIMIT_EXCEEDED: CostLimitExceededEvent,
    EventType.SECURITY_CHECK_PASSED: SecurityCheckPassedEvent,
    EventType.SECURITY_CHECK_FAILED: SecurityCheckFailedEvent,
    EventType.SAGA_EVENT: SagaEvent,
}


# ============================================================================
# Event Store Implementation
# ============================================================================

@dataclass
class InfrastructureState:
    """
    Reconstructed state from event log.
    
    This represents the current state of all jobs and instances,
    computed by replaying all events.
    """
    jobs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    instances: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    costs_by_provider: Dict[str, float] = field(default_factory=dict)
    costs_by_job: Dict[str, float] = field(default_factory=dict)
    active_instances: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0


# ============================================================================
# Abstract Event Store (OpenHands Pattern)
# ============================================================================

class EventStoreABC(ABC):
    """
    Abstract base class for event stores.
    
    Defines the interface that all event store backends must implement.
    This allows for multiple storage backends:
    - FileEventStore: JSON Lines file storage
    - MemoryEventStore: In-memory for testing
    - DatabaseEventStore: PostgreSQL/SQLite backend
    - RemoteEventStore: Cloud-based event log (GCS, S3)
    
    OpenHands-inspired design with search_events and get_event methods.
    """
    
    @abstractmethod
    def append(self, event: InfrastructureEvent) -> int:
        """
        Append an event to the store.
        
        Args:
            event: Event to append
            
        Returns:
            sequence_id assigned to the event
        """
        pass
    
    @abstractmethod
    def get_event(self, sequence_id: int) -> Optional[InfrastructureEvent]:
        """
        Get a specific event by sequence ID.
        
        Args:
            sequence_id: Sequential event ID
            
        Returns:
            Event if found, None otherwise
        """
        pass
    
    @abstractmethod
    def search_events(
        self,
        filter: Optional[EventFilter] = None
    ) -> Iterable[InfrastructureEvent]:
        """
        Search events with optional filter.
        
        Args:
            filter: EventFilter with query criteria
            
        Returns:
            Iterable of matching events
        """
        pass
    
    @abstractmethod
    def get_latest_sequence_id(self) -> int:
        """Get the latest sequence ID in the store."""
        pass
    
    @abstractmethod
    def reconstruct_state(self) -> InfrastructureState:
        """Reconstruct current state by replaying all events."""
        pass
    
    def get_events_for_job(self, job_id: str) -> List[InfrastructureEvent]:
        """Convenience: Get all events for a job."""
        filter = EventFilter(job_id=job_id)
        return list(self.search_events(filter))
    
    def get_events_for_instance(self, instance_id: str) -> List[InfrastructureEvent]:
        """Convenience: Get all events for an instance."""
        filter = EventFilter(instance_id=instance_id)
        return list(self.search_events(filter))


# ============================================================================
# Cache Page for Large Event Volumes (OpenHands Pattern)
# ============================================================================

@dataclass(frozen=True)
class _CachePage:
    """
    Immutable cache page for efficient event storage.
    
    Events are stored in pages to allow efficient memory management
    when dealing with millions of events.
    """
    start_sequence_id: int
    end_sequence_id: int
    events: tuple  # Immutable tuple of events
    
    def contains(self, sequence_id: int) -> bool:
        return self.start_sequence_id <= sequence_id <= self.end_sequence_id


class FileEventStore(EventStoreABC):
    """
    File-based event store using JSON Lines format.
    
    Implements EventStoreABC with local file storage.
    Supports dependency injection of FileStore for testing.
    
    Features:
    - Thread-safe append operations
    - JSON Lines format for easy parsing and streaming
    - Page-based caching for large event volumes
    - Automatic sequence ID assignment
    - State reconstruction from event log
    
    Example:
        store = FileEventStore("events.jsonl")
        seq_id = store.append(JobRequestedEvent(job_id="abc"))
        event = store.get_event(seq_id)
    """
    
    PAGE_SIZE = 1000  # Events per cache page
    
    def __init__(
        self,
        storage_path: Union[str, Path] = "infrastructure_events.jsonl",
        file_store: Optional[FileStore] = None,
        max_pages_in_memory: int = 10,
        auto_flush: bool = True,
    ):
        """
        Initialize file-based event store.
        
        Args:
            storage_path: Path to event log file (JSON Lines format)
            file_store: Storage backend (default: LocalFileStore)
            max_pages_in_memory: Maximum cache pages to keep in memory
            auto_flush: Automatically flush to disk after each append
        """
        self.storage_path = str(storage_path)
        self.file_store = file_store or LocalFileStore()
        self.max_pages_in_memory = max_pages_in_memory
        self.auto_flush = auto_flush
        
        # Sequence counter
        self._next_sequence_id = 1
        
        # Page-based cache
        self._pages: Dict[int, _CachePage] = {}  # page_num -> CachePage
        self._current_page_events: List[InfrastructureEvent] = []
        
        # Thread safety
        self._lock = threading.Lock()
        
        # Load existing events if file exists
        if self.file_store.exists(self.storage_path):
            self._load_events()
    
    def _load_events(self) -> None:
        """Load events from disk into page cache."""
        try:
            content = self.file_store.read(self.storage_path)
            events = []
            
            for line in content.strip().split("\n"):
                if line:
                    data = json.loads(line)
                    event = InfrastructureEvent.from_dict(data)
                    events.append(event)
                    
                    # Track max sequence_id
                    if event.sequence_id >= self._next_sequence_id:
                        self._next_sequence_id = event.sequence_id + 1
            
            # Build pages from loaded events
            for i in range(0, len(events), self.PAGE_SIZE):
                page_events = events[i:i + self.PAGE_SIZE]
                if page_events:
                    page_num = i // self.PAGE_SIZE
                    self._pages[page_num] = _CachePage(
                        start_sequence_id=page_events[0].sequence_id,
                        end_sequence_id=page_events[-1].sequence_id,
                        events=tuple(page_events)
                    )
            
            # Current page is the last incomplete page
            last_page_start = (len(events) // self.PAGE_SIZE) * self.PAGE_SIZE
            if last_page_start < len(events):
                self._current_page_events = events[last_page_start:]
            
        except Exception as e:
            print(f"Warning: Failed to load events: {e}")
    
    def append(self, event: InfrastructureEvent) -> int:
        """
        Append an event to the store.
        
        Assigns a sequential ID and persists to disk.
        
        Args:
            event: Event to append
            
        Returns:
            sequence_id assigned to the event
        """
        with self._lock:
            # Assign sequence ID
            event.sequence_id = self._next_sequence_id
            self._next_sequence_id += 1
            
            # Add to current page
            self._current_page_events.append(event)
            
            # Check if we need to create a new page
            if len(self._current_page_events) >= self.PAGE_SIZE:
                self._flush_current_page()
            
            # Write to disk
            if self.auto_flush:
                self._write_event(event)
            
            return event.sequence_id
    
    def _flush_current_page(self) -> None:
        """Convert current events to immutable page."""
        if not self._current_page_events:
            return
        
        page_num = len(self._pages)
        self._pages[page_num] = _CachePage(
            start_sequence_id=self._current_page_events[0].sequence_id,
            end_sequence_id=self._current_page_events[-1].sequence_id,
            events=tuple(self._current_page_events)
        )
        self._current_page_events = []
        
        # Evict old pages if too many
        while len(self._pages) > self.max_pages_in_memory:
            oldest = min(self._pages.keys())
            del self._pages[oldest]
    
    def _write_event(self, event: InfrastructureEvent) -> None:
        """Write single event to disk."""
        try:
            self.file_store.append(self.storage_path, event.to_json() + "\n")
        except Exception as e:
            print(f"Warning: Failed to write event: {e}")
    
    def get_event(self, sequence_id: int) -> Optional[InfrastructureEvent]:
        """
        Get a specific event by sequence ID.
        
        Args:
            sequence_id: Sequential event ID
            
        Returns:
            Event if found, None otherwise
        """
        with self._lock:
            # Check current page first
            for event in self._current_page_events:
                if event.sequence_id == sequence_id:
                    return event
            
            # Check cached pages
            for page in self._pages.values():
                if page.contains(sequence_id):
                    for event in page.events:
                        if event.sequence_id == sequence_id:
                            return event
            
            # Need to load from disk if not in cache
            # (simplified - in production would load specific page)
            return None
    
    def search_events(
        self,
        filter: Optional[EventFilter] = None
    ) -> Iterable[InfrastructureEvent]:
        """
        Search events with optional filter.
        
        Args:
            filter: EventFilter with query criteria
            
        Returns:
            Generator of matching events
        """
        with self._lock:
            events = self._get_all_events()
        
        count = 0
        for event in events:
            if filter is None or filter.matches(event):
                yield event
                count += 1
                if filter and filter.limit and count >= filter.limit:
                    break
    
    def _get_all_events(self) -> List[InfrastructureEvent]:
        """Get all events from cache and current page."""
        events = []
        
        # Get events from pages in order
        for page_num in sorted(self._pages.keys()):
            events.extend(self._pages[page_num].events)
        
        # Add current page
        events.extend(self._current_page_events)
        
        return events
    
    def get_latest_sequence_id(self) -> int:
        """Get the latest sequence ID in the store."""
        return self._next_sequence_id - 1
    
    def get_events(
        self,
        event_types: Optional[List[EventType]] = None,
        job_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        provider: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[InfrastructureEvent]:
        """
        Query events with filters (legacy API, use search_events for new code).
        """
        filter = EventFilter(
            event_types=event_types,
            job_id=job_id,
            instance_id=instance_id,
            provider=provider,
            since=since,
            limit=limit,
        )
        return list(self.search_events(filter))
    
    def get_events_for_instance(self, instance_id: str) -> List[InfrastructureEvent]:
        """Get all events for a specific instance."""
        return self.get_events(instance_id=instance_id)
    
    def reconstruct_state(self) -> InfrastructureState:
        """
        Reconstruct current state by replaying all events.
        
        Returns:
            InfrastructureState representing current system state
        """
        state = InfrastructureState()
        
        with self._lock:
            for event in self._get_all_events():
                _apply_event_to_state(state, event)
        
        return state
    
    def get_cost_summary(self) -> Dict[str, Any]:
        """Get cost summary from events."""
        state = self.reconstruct_state()
        return {
            "total_cost_usd": state.total_cost_usd,
            "costs_by_provider": state.costs_by_provider,
            "costs_by_job": state.costs_by_job,
            "active_instances": state.active_instances,
            "completed_jobs": state.completed_jobs,
            "failed_jobs": state.failed_jobs,
        }
    
    def clear(self) -> None:
        """Clear all events (use with caution!)."""
        with self._lock:
            self._pages.clear()
            self._current_page_events.clear()
            self._next_sequence_id = 1
            if self.file_store.exists(self.storage_path):
                self.file_store.delete(self.storage_path)
    
    def __len__(self) -> int:
        """Number of events in store."""
        count = sum(len(page.events) for page in self._pages.values())
        count += len(self._current_page_events)
        return count
    
    def __repr__(self) -> str:
        return f"<FileEventStore events={len(self)} path={self.storage_path}>"


# ============================================================================
# Memory Event Store (For Testing)
# ============================================================================

class InMemoryFileStore:
    """In-memory file store for testing without disk I/O."""
    
    def __init__(self):
        self._files: Dict[str, str] = {}
    
    def read(self, path: str) -> str:
        return self._files.get(path, "")
    
    def write(self, path: str, content: str) -> None:
        self._files[path] = content
    
    def append(self, path: str, content: str) -> None:
        self._files[path] = self._files.get(path, "") + content
    
    def exists(self, path: str) -> bool:
        return path in self._files
    
    def delete(self, path: str) -> None:
        self._files.pop(path, None)


class MemoryEventStore(EventStoreABC):
    """
    Pure in-memory event store for testing.
    
    Fast, isolated, no disk I/O. Perfect for unit tests.
    
    Example:
        store = MemoryEventStore()
        store.append(JobRequestedEvent(job_id="test"))
        assert len(store) == 1
    """
    
    def __init__(self):
        self._events: List[InfrastructureEvent] = []
        self._next_sequence_id = 1
        self._lock = threading.Lock()
    
    def append(self, event: InfrastructureEvent) -> int:
        with self._lock:
            event.sequence_id = self._next_sequence_id
            self._next_sequence_id += 1
            self._events.append(event)
            return event.sequence_id
    
    def get_event(self, sequence_id: int) -> Optional[InfrastructureEvent]:
        with self._lock:
            for event in self._events:
                if event.sequence_id == sequence_id:
                    return event
            return None
    
    def search_events(
        self,
        filter: Optional[EventFilter] = None
    ) -> Iterable[InfrastructureEvent]:
        with self._lock:
            events = self._events.copy()
        
        count = 0
        for event in events:
            if filter is None or filter.matches(event):
                yield event
                count += 1
                if filter and filter.limit and count >= filter.limit:
                    break
    
    def get_latest_sequence_id(self) -> int:
        return self._next_sequence_id - 1
    
    def reconstruct_state(self) -> InfrastructureState:
        state = InfrastructureState()
        with self._lock:
            for event in self._events:
                _apply_event_to_state(state, event)
        return state
    
    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._next_sequence_id = 1
    
    def __len__(self) -> int:
        return len(self._events)
    
    def __repr__(self) -> str:
        return f"<MemoryEventStore events={len(self)}>"


def _apply_event_to_state(state: InfrastructureState, event: InfrastructureEvent) -> None:
    """
    Shared state update logic.
    
    Extracted to avoid code duplication between FileEventStore and MemoryEventStore.
    """
    # Job events
    if isinstance(event, JobRequestedEvent):
        state.jobs[event.job_id] = {
            "status": "requested",
            "worker_type": event.worker_type,
            "gpu_type": event.gpu_type,
            "created_at": event.timestamp,
            "cost": 0.0,
        }
    
    elif isinstance(event, JobQueuedEvent):
        if event.job_id in state.jobs:
            state.jobs[event.job_id]["status"] = "queued"
            state.jobs[event.job_id]["queue_position"] = event.queue_position
    
    elif isinstance(event, ProvisioningSucceededEvent):
        if event.job_id in state.jobs:
            state.jobs[event.job_id]["status"] = "running"
            state.jobs[event.job_id]["instance_id"] = event.instance_id
        
        state.instances[event.instance_id] = {
            "status": "running",
            "provider": event.provider,
            "job_id": event.job_id,
            "price_per_hour": event.price_per_hour,
            "started_at": event.timestamp,
            "cost": 0.0,
        }
        state.active_instances += 1
    
    elif isinstance(event, InstanceTerminatedEvent):
        if event.instance_id in state.instances:
            state.instances[event.instance_id]["status"] = "terminated"
            state.instances[event.instance_id]["cost"] = event.final_cost
            state.active_instances = max(0, state.active_instances - 1)
    
    elif isinstance(event, CostIncurredEvent):
        state.total_cost_usd += event.amount_usd
        
        if event.provider:
            state.costs_by_provider[event.provider] = \
                state.costs_by_provider.get(event.provider, 0) + event.amount_usd
        
        if event.job_id:
            state.costs_by_job[event.job_id] = \
                state.costs_by_job.get(event.job_id, 0) + event.amount_usd
            
            if event.job_id in state.jobs:
                state.jobs[event.job_id]["cost"] += event.amount_usd
    
    elif isinstance(event, JobCompletedEvent):
        if event.job_id in state.jobs:
            state.jobs[event.job_id]["status"] = "completed"
            state.jobs[event.job_id]["final_cost"] = event.final_cost
        state.completed_jobs += 1
    
    elif isinstance(event, JobFailedEvent):
        if event.job_id in state.jobs:
            state.jobs[event.job_id]["status"] = "failed"
            state.jobs[event.job_id]["error"] = event.error_message
        state.failed_jobs += 1
    
    elif isinstance(event, InstancePreemptedEvent):
        if event.instance_id in state.instances:
            state.instances[event.instance_id]["status"] = "preempted"
            state.active_instances = max(0, state.active_instances - 1)


# ============================================================================
# Global Event Store Instance (Optional Singleton)
# ============================================================================

_global_event_store: Optional[FileEventStore] = None


def get_event_store(
    storage_path: Union[str, Path] = "infrastructure_events.jsonl"
) -> FileEventStore:
    """
    Get or create the global event store instance.
    
    Args:
        storage_path: Path to event log file
        
    Returns:
        Global FileEventStore instance
    """
    global _global_event_store
    
    if _global_event_store is None:
        _global_event_store = FileEventStore(storage_path)
    
    return _global_event_store


# Backward compatibility alias
InfrastructureEventStore = FileEventStore
