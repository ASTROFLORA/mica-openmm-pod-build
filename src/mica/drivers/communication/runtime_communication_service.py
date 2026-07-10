"""
Runtime Communication Service — extraction from AgenticDriver.

Owns communication protocol bootstrap and runtime telemetry emission.
"""

from uuid import UUID
from typing import Any, Callable, Dict, List, Optional, Sequence

from bsm.communication.core import Topic

from .async_specialist_debate import AsyncSpecialistDebateBackbone
from .runtime_subscriber_registry import RuntimeMessageBusSubscriberRegistry


class RuntimeCommunicationService:
    """Encapsulates communication protocol and runtime telemetry surfaces."""

    def __init__(
        self,
        *,
        persist_store_fn: Callable[..., None],
        build_runtime_telemetry_emitter_fn: Callable[..., Any],
        emit_runtime_status_fn: Callable[..., Any],
        emit_runtime_error_fn: Callable[..., Any],
        persona_system: Any,
        subscriber_registry: Optional[RuntimeMessageBusSubscriberRegistry] = None,
    ) -> None:
        self._persist_store_fn = persist_store_fn
        self._build_runtime_telemetry_emitter_fn = build_runtime_telemetry_emitter_fn
        self._emit_runtime_status_fn = emit_runtime_status_fn
        self._emit_runtime_error_fn = emit_runtime_error_fn
        self._persona_system = persona_system
        self._subscriber_registry = subscriber_registry or RuntimeMessageBusSubscriberRegistry()

        self._communication_protocol: Any = None
        self._communication_bus: Any = None
        self._runtime_telemetry_emitter: Any = None

    def get_or_create_communication_protocol(self) -> Any:
        protocol = self._communication_protocol
        if protocol is not None:
            return protocol
        try:
            from bsm.communication.core import CommunicationProtocol, MessageBus, MessageStore

            bus = MessageBus(store=MessageStore())
            self._subscriber_registry.install(bus)
            protocol = CommunicationProtocol(bus=bus)
            self._communication_protocol = protocol
            self._communication_bus = bus
            return protocol
        except Exception:
            return None

    def get_or_create_communication_bus(self) -> Any:
        self.get_or_create_communication_protocol()
        return self._communication_bus

    def create_specialist_debate_backbone(self) -> Optional[AsyncSpecialistDebateBackbone]:
        bus = self.get_or_create_communication_bus()
        if bus is None:
            return None
        return AsyncSpecialistDebateBackbone(bus=bus)

    def persist_communication_store(self, *, checkpoint_dir: str, session_id: str) -> Any:
        persisted_path = self._persist_store_fn(bus=self._communication_bus, checkpoint_dir=checkpoint_dir, session_id=session_id)
        self._subscriber_registry.note_persisted_delivery_evidence(persisted_path)
        return persisted_path

    def get_runtime_subscriber_statuses(self) -> List[Dict[str, Any]]:
        return self._subscriber_registry.snapshot_statuses()

    def register_runtime_subscriber(
        self,
        *,
        subscriber_id: str,
        topics: Sequence[Topic | str],
        handler: Callable[..., Any],
    ) -> None:
        self._subscriber_registry.register_subscriber(
            subscriber_id=subscriber_id,
            topics=topics,
            handler=handler,
        )
        if self._communication_bus is not None:
            self._subscriber_registry.install(self._communication_bus)

    def get_or_create_runtime_telemetry_emitter(self) -> Any:
        emitter = self._runtime_telemetry_emitter
        if emitter is not None:
            return emitter
        try:
            self.get_or_create_communication_protocol()
            emitter = self._build_runtime_telemetry_emitter_fn(
                message_bus=self._communication_bus,
                persona=self._persona_system,
                roadmap_phase="runtime.process_agentic_prompt",
                goal="Project driver lifecycle telemetry onto the compatibility bus.",
                agent_name="driver",
                subsystem="runtime",
            )
            self._runtime_telemetry_emitter = emitter
            return emitter
        except Exception:
            return None

    async def emit_runtime_status(
        self,
        *,
        checkpoint_dir: str,
        session_id: str,
        run_id: str,
        phase: str,
        status: str,
        details: Optional[str] = None,
        mode: Optional[str] = None,
        severity: str = "info",
        metrics: Optional[Dict[str, Any]] = None,
        artifact_refs: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
    ) -> None:
        emitter = self.get_or_create_runtime_telemetry_emitter()
        if emitter is None:
            return
        raw_status = str(status or "").strip().lower()
        normalized_status = raw_status
        if normalized_status not in {"started", "in_progress", "completed", "failed"}:
            if normalized_status in {"accept", "allow", "allowed", "success", "succeeded", "ok"}:
                normalized_status = "completed"
            elif normalized_status in {"block", "blocked", "error", "critical"}:
                normalized_status = "failed"
            else:
                normalized_status = "in_progress"
        normalized_metrics = dict(metrics or {})
        if raw_status and raw_status != normalized_status:
            normalized_metrics.setdefault("raw_status", raw_status)

        await self._emit_runtime_status_fn(
            emitter=emitter,
            session_id=session_id,
            run_id=run_id,
            phase=phase,
            status=normalized_status,
            details=details,
            mode=mode,
            severity=severity,
            metrics=normalized_metrics,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            source_ids=source_ids,
        )
        self.persist_communication_store(checkpoint_dir=checkpoint_dir, session_id=session_id)

    async def emit_runtime_error(
        self,
        *,
        checkpoint_dir: str,
        session_id: str,
        run_id: str,
        phase: str,
        error_type: str,
        message: str,
        traceback_text: Optional[str] = None,
        artifact_path: Optional[str] = None,
        rescue_suggestion: Optional[str] = None,
        mode: Optional[str] = None,
        retryable: Optional[bool] = None,
        artifact_refs: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> None:
        emitter = self.get_or_create_runtime_telemetry_emitter()
        if emitter is None:
            return
        await self._emit_runtime_error_fn(
            emitter=emitter,
            session_id=session_id,
            run_id=run_id,
            phase=phase,
            error_type=error_type,
            message=message,
            traceback_text=traceback_text,
            artifact_path=artifact_path,
            rescue_suggestion=rescue_suggestion,
            mode=mode,
            retryable=retryable,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
        )
        self.persist_communication_store(checkpoint_dir=checkpoint_dir, session_id=session_id)

    async def publish_protocol_node_receipt(
        self,
        *,
        checkpoint_dir: str,
        session_id: str,
        run_id: str,
        agent_name: str,
        receipt: Any,
    ) -> Optional[UUID]:
        protocol = self.get_or_create_communication_protocol()
        if protocol is None:
            return None
        publish_fn = getattr(protocol, "publish_protocol_node_receipt", None)
        if not callable(publish_fn):
            return None
        message_id = await publish_fn(
            session_id=session_id,
            run_id=run_id,
            agent_name=agent_name,
            receipt=receipt,
        )
        self.persist_communication_store(checkpoint_dir=checkpoint_dir, session_id=session_id)
        return message_id

    async def publish_shared_kernel_snapshot(
        self,
        *,
        checkpoint_dir: str,
        session_id: str,
        run_id: str,
        agent_name: str,
        snapshot: Dict[str, Any],
    ) -> Optional[UUID]:
        protocol = self.get_or_create_communication_protocol()
        if protocol is None:
            return None
        publish_fn = getattr(protocol, "publish_shared_kernel_snapshot", None)
        if not callable(publish_fn):
            return None
        message_id = await publish_fn(
            session_id=session_id,
            run_id=run_id,
            agent_name=agent_name,
            snapshot=snapshot,
        )
        self.persist_communication_store(checkpoint_dir=checkpoint_dir, session_id=session_id)
        return message_id

    async def publish_protocol_run_receipt(
        self,
        *,
        checkpoint_dir: str,
        session_id: str,
        run_id: str,
        agent_name: str,
        receipt: Any,
    ) -> Optional[UUID]:
        protocol = self.get_or_create_communication_protocol()
        if protocol is None:
            return None
        publish_fn = getattr(protocol, "publish_protocol_run_receipt", None)
        if not callable(publish_fn):
            return None
        message_id = await publish_fn(
            session_id=session_id,
            run_id=run_id,
            agent_name=agent_name,
            receipt=receipt,
        )
        self.persist_communication_store(checkpoint_dir=checkpoint_dir, session_id=session_id)
        return message_id

    async def publish_unified_protocol_runtime(
        self,
        *,
        checkpoint_dir: str,
        session_id: str,
        run_id: str,
        agent_name: str,
        unified_runtime: Dict[str, Any],
    ) -> Optional[UUID]:
        protocol = self.get_or_create_communication_protocol()
        if protocol is None:
            return None
        publish_fn = getattr(protocol, "publish_unified_protocol_runtime", None)
        if not callable(publish_fn):
            return None
        message_id = await publish_fn(
            session_id=session_id,
            run_id=run_id,
            agent_name=agent_name,
            unified_runtime=unified_runtime,
        )
        self.persist_communication_store(checkpoint_dir=checkpoint_dir, session_id=session_id)
        return message_id
