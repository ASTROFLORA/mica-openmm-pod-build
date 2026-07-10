"""Runtime MessageBus subscriber registry for MO-02.

Activates a bounded floor of non-driver runtime subscribers on the
compatibility MessageBus so protocol and shared-kernel events can be observed
without driver-only forwarding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any, Callable, Dict, List, Optional, Sequence

from bsm.communication.core import Topic, TopicRegistry


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _callable_ref(handler: Callable[..., Any]) -> str:
    module = getattr(handler, "__module__", "")
    qualname = getattr(handler, "__qualname__", getattr(handler, "__name__", repr(handler)))
    return f"{module}.{qualname}" if module else qualname


def _message_context(message: Any, key: str) -> Dict[str, Any]:
    payload = getattr(message, "context", None)
    if not isinstance(payload, dict):
        return {}
    candidate = payload.get(key)
    return dict(candidate) if isinstance(candidate, dict) else {}


SubscriberHandler = Callable[[Any, Dict[str, Any], "RuntimeSubscriberStatus"], Any]


@dataclass
class RuntimeSubscriberStatus:
    subscriber_id: str
    topics: List[str]
    handler_ref: str
    status: str = "idle"
    last_error: str = ""
    last_message_id: str = ""
    last_topic: str = ""
    last_delivery_at: str = ""
    delivery_count: int = 0
    persisted_delivery_evidence_path: str = ""
    observed_state: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subscriber_id": self.subscriber_id,
            "topics": list(self.topics),
            "handler_ref": self.handler_ref,
            "status": self.status,
            "last_error": self.last_error,
            "last_message_id": self.last_message_id,
            "last_topic": self.last_topic,
            "last_delivery_at": self.last_delivery_at,
            "delivery_count": self.delivery_count,
            "persisted_delivery_evidence_path": self.persisted_delivery_evidence_path,
            "observed_state": dict(self.observed_state),
        }


@dataclass(frozen=True)
class _SubscriberDefinition:
    subscriber_id: str
    topics: tuple[Topic, ...]
    handler: SubscriberHandler


class RuntimeMessageBusSubscriberRegistry:
    """Bounded registry for non-driver runtime subscribers."""

    def __init__(self, *, enable_defaults: bool = True) -> None:
        self._definitions: Dict[str, _SubscriberDefinition] = {}
        self._statuses: Dict[str, RuntimeSubscriberStatus] = {}
        self._installed_buses: Dict[int, Any] = {}
        self._installed_pairs: set[tuple[int, str]] = set()
        if enable_defaults:
            self._register_default_subscribers()

    def _register_default_subscribers(self) -> None:
        runtime_topics = [
            TopicRegistry.RUNTIME_SHARED_KERNEL,
            TopicRegistry.RUNTIME_PROTOCOL_NODE,
            TopicRegistry.RUNTIME_PROTOCOL_UNIFIED,
        ]
        self.register_subscriber(
            subscriber_id="biodynamo_execution_runtime_subscriber",
            topics=runtime_topics,
            handler=self._observe_biodynamo_runtime,
        )
        self.register_subscriber(
            subscriber_id="smic_metric_runtime_subscriber",
            topics=runtime_topics,
            handler=self._observe_smic_metric_runtime,
        )
        self.register_subscriber(
            subscriber_id="cue_evidence_runtime_subscriber",
            topics=runtime_topics,
            handler=self._observe_cue_evidence_runtime,
        )

    def register_subscriber(
        self,
        *,
        subscriber_id: str,
        topics: Sequence[Topic | str],
        handler: SubscriberHandler,
    ) -> None:
        normalized_topics = tuple(Topic(str(topic)) for topic in topics)
        self._definitions[subscriber_id] = _SubscriberDefinition(
            subscriber_id=subscriber_id,
            topics=normalized_topics,
            handler=handler,
        )
        self._statuses[subscriber_id] = RuntimeSubscriberStatus(
            subscriber_id=subscriber_id,
            topics=[str(topic) for topic in normalized_topics],
            handler_ref=_callable_ref(handler),
        )
        for bus in self._installed_buses.values():
            self._install_definition(bus, self._definitions[subscriber_id])

    def install(self, bus: Any) -> None:
        if bus is None:
            return
        self._installed_buses[id(bus)] = bus
        for definition in self._definitions.values():
            self._install_definition(bus, definition)

    def note_persisted_delivery_evidence(self, path: Any) -> None:
        if not path:
            return
        evidence_path = str(path)
        for status in self._statuses.values():
            if status.delivery_count > 0:
                status.persisted_delivery_evidence_path = evidence_path

    def snapshot_statuses(self) -> List[Dict[str, Any]]:
        return [self._statuses[key].to_dict() for key in sorted(self._statuses)]

    def _install_definition(self, bus: Any, definition: _SubscriberDefinition) -> None:
        install_key = (id(bus), definition.subscriber_id)
        if install_key in self._installed_pairs:
            return
        status = self._statuses[definition.subscriber_id]
        for topic in definition.topics:
            bus.subscribe(topic, self._wrap_handler(topic=topic, handler=definition.handler, status=status))
        self._installed_pairs.add(install_key)

    @staticmethod
    def _wrap_handler(*, topic: Topic, handler: SubscriberHandler, status: RuntimeSubscriberStatus) -> Callable[..., Any]:
        async def _wrapped(message: Any, context: Dict[str, Any]) -> None:
            header = getattr(message, "header", None)
            status.last_message_id = str(getattr(header, "message_id", "") or "")
            status.last_topic = str(context.get("topic") or topic)
            status.last_delivery_at = _timestamp_utc()
            try:
                outcome = handler(message, context, status)
                if isawaitable(outcome):
                    await outcome
            except Exception as exc:
                status.status = "error"
                status.last_error = str(exc)
                raise
            else:
                status.status = "delivered"
                status.last_error = ""
                status.delivery_count += 1

        return _wrapped

    @staticmethod
    def _observe_biodynamo_runtime(message: Any, _context: Dict[str, Any], status: RuntimeSubscriberStatus) -> None:
        node_receipt = _message_context(message, "protocol_node_receipt")
        snapshot = _message_context(message, "shared_kernel_snapshot")
        unified_runtime = _message_context(message, "unified_protocol_runtime")
        if snapshot:
            status.observed_state["last_snapshot_id"] = str(snapshot.get("snapshot_id") or "")
            status.observed_state["last_snapshot_user_id"] = str(snapshot.get("user_id") or "default")
        if node_receipt:
            status.observed_state["last_protocol_id"] = str(node_receipt.get("protocol_id") or "")
            status.observed_state["last_execution_node_id"] = str(node_receipt.get("node_id") or "")
            status.observed_state["last_execution_surface"] = str(node_receipt.get("actor_surface") or "")
        if unified_runtime:
            status.observed_state["last_protocol_id"] = str(unified_runtime.get("protocol_id") or "")
            status.observed_state["last_graph_run_status"] = str(unified_runtime.get("graph_run_status") or "")

    @staticmethod
    def _observe_smic_metric_runtime(message: Any, _context: Dict[str, Any], status: RuntimeSubscriberStatus) -> None:
        snapshot = _message_context(message, "shared_kernel_snapshot")
        node_receipt = _message_context(message, "protocol_node_receipt")
        unified_runtime = _message_context(message, "unified_protocol_runtime")
        if snapshot:
            status.observed_state["last_mu"] = float(snapshot.get("mu") or 0.0)
            status.observed_state["last_sigma"] = float(snapshot.get("sigma") or 0.0)
            status.observed_state["last_contributing_quintuples"] = int(snapshot.get("contributing_quintuples") or 0)
        if node_receipt:
            status.observed_state["last_event_type"] = str(node_receipt.get("event_type") or "")
            status.observed_state["last_actor_id"] = str(node_receipt.get("actor_id") or "")
        if unified_runtime:
            status.observed_state["last_projection_only"] = bool(unified_runtime.get("projection_only", False))
            status.observed_state["last_node_receipt_count"] = len(list(unified_runtime.get("node_receipts") or []))

    @staticmethod
    def _observe_cue_evidence_runtime(message: Any, _context: Dict[str, Any], status: RuntimeSubscriberStatus) -> None:
        snapshot = _message_context(message, "shared_kernel_snapshot")
        node_receipt = _message_context(message, "protocol_node_receipt")
        unified_runtime = _message_context(message, "unified_protocol_runtime")
        intent = getattr(message, "intent", None)
        status.observed_state["last_required_evidence"] = list(getattr(intent, "required_evidence", []) or [])
        status.observed_state["last_evidence_refs"] = list(getattr(message, "evidence_refs", []) or [])
        status.observed_state["last_artifact_refs"] = list(getattr(message, "artifact_refs", []) or [])
        if snapshot:
            status.observed_state["last_snapshot_id"] = str(snapshot.get("snapshot_id") or "")
        if node_receipt:
            status.observed_state["last_protocol_node_id"] = str(node_receipt.get("node_id") or "")
            status.observed_state["last_protocol_event_type"] = str(node_receipt.get("event_type") or "")
        if unified_runtime:
            status.observed_state["last_protocol_node_count"] = int(unified_runtime.get("node_policy_summary", {}).get("node_count") or 0)


__all__ = [
    "RuntimeMessageBusSubscriberRegistry",
    "RuntimeSubscriberStatus",
]