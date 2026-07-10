"""R25.5 EventBus — tiny pub/sub over :mod:`mica.agentic.events` payloads.

Activates the graph-visible ``agentic_driver.py ↔ events.py`` edge (w=988) as
a cognitive bus by turning ``events.py`` into a real cross-community channel
with at least one non-driver publisher and one non-driver subscriber.

Design constraints (from R25.5 prompt + R23 CCC verdict):
- In-process only. No external dependencies. Thread-safe publish/subscribe.
- Per-event-type subscriber lists. Unknown events are silently ignored.
- Subscriber exceptions are logged and swallowed — the bus is best-effort
  (a cognitive side-channel must NEVER break the primary write path).
- No mutation of events (they are frozen dataclasses). Subscribers receive
  the original instance.

Publishers target: ``mica.memory.atom.persistence_timescale`` (w=988 latent
amplifier, confirmed alive in the graph extract).

Subscribers target: ``mica.agentic.cue_evaluator`` (currently deg=0 in the
live wire-graph; gets its first non-driver inbound edge via this bus), plus
the Tolomeo P0-A MUDO subscriber for durable runtime provenance writes.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, Dict, List, Type

from .events import LoopEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[[LoopEvent], None]


class EventBus:
    """Thread-safe in-process event bus.

    Intentionally minimal: this is the activation slice for R25.5, not a
    production replacement for a real broker. If the bus is replaced later,
    the contract is: ``publish(event)`` never raises, subscribers see the
    same dataclass instance they registered for.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: Dict[Type[LoopEvent], List[EventHandler]] = defaultdict(list)
        self._published: int = 0  # R25.5 witness counter

    def subscribe(self, event_type: Type[LoopEvent], handler: EventHandler) -> None:
        """Register ``handler`` for events of exactly ``event_type``."""
        with self._lock:
            self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: Type[LoopEvent], handler: EventHandler) -> None:
        """Remove ``handler`` if present. Safe if never registered."""
        with self._lock:
            handlers = self._handlers.get(event_type) or []
            if handler in handlers:
                handlers.remove(handler)

    def publish(self, event: LoopEvent) -> int:
        """Dispatch ``event`` to all subscribers of its concrete type.

        Returns the number of successfully invoked subscribers. Handler
        exceptions are logged and swallowed. Thread-safe.
        """
        delivered = 0
        with self._lock:
            handlers = list(self._handlers.get(type(event)) or [])
            self._published += 1
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[EVENT_BUS] subscriber %s raised on %s: %s",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(event).__name__,
                    exc,
                )
        return delivered

    @property
    def total_published(self) -> int:
        """Total number of publish() calls since construction. Witness metric."""
        return self._published


# --------------------------------------------------------------------------- #
# Process-global singleton (opt-in via get_event_bus())                        #
# --------------------------------------------------------------------------- #

_GLOBAL_BUS: EventBus | None = None
_GLOBAL_BUS_LOCK = threading.Lock()


def get_event_bus() -> EventBus:
    """Return the process-global bus, creating it lazily on first call.

    On first construction, the R26.5 :class:`GovernanceCoordinator` is
    auto-installed as a subscriber so that every ``SnapshotPersisted``
    publication drives the five governance module wires. Installation
    failure is swallowed — the bus remains usable without governance.

    Callers that want isolation (e.g., tests, multi-tenant subloops) should
    construct their own :class:`EventBus` instead of using this singleton.
    """
    global _GLOBAL_BUS
    if _GLOBAL_BUS is None:
        with _GLOBAL_BUS_LOCK:
            if _GLOBAL_BUS is None:
                bus = EventBus()
                try:
                    from .governance_coordinator import GovernanceCoordinator

                    coordinator = GovernanceCoordinator()
                    coordinator.bind_event_bus(bus)
                    # Stash for test introspection; not a public API.
                    bus._r26_5_governance_coordinator = coordinator  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[EVENT_BUS] governance auto-install skipped: %s", exc)
                try:
                    from .mudo_event_subscriber import MUDOEventSubscriber

                    mudo_subscriber = MUDOEventSubscriber()
                    mudo_subscriber.bind_event_bus(bus)
                    bus._tolomeo_p0_mudo_subscriber = mudo_subscriber  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[EVENT_BUS] MUDO event subscriber auto-install skipped: %s", exc)
                _GLOBAL_BUS = bus
    return _GLOBAL_BUS


def reset_event_bus() -> None:
    """Drop the global bus. Test-only."""
    global _GLOBAL_BUS
    with _GLOBAL_BUS_LOCK:
        _GLOBAL_BUS = None


async def drain_mudo_subscriber_tasks(bus: EventBus) -> dict[str, object]:
    """Drain the default MUDO durable subscriber if it is installed on ``bus``."""
    subscriber = getattr(bus, "_tolomeo_p0_mudo_subscriber", None)
    if subscriber is None:
        return {"subscriber_present": False, "drained": False, "errors": []}

    drain = getattr(subscriber, "drain_tasks", None)
    if drain is None:
        return {
            "subscriber_present": True,
            "drained": False,
            "errors": ["subscriber_missing_drain_tasks"],
        }

    results = list(await drain())
    return {
        "subscriber_present": True,
        "drained": True,
        "task_count": len(results),
        "errors": [str(result) for result in results if isinstance(result, Exception)],
    }
