"""Telemetry emitters for runtime observability projections."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from .message_schema import (
    AgentMessage,
    AgentPersona,
    ErrorPayload,
    MessageHeader,
    MessageType,
    ResearchIntent,
    SafetyTier,
    StatusPayload,
)
from .protocol import MessageBus, MessageStore, Topic, TopicRegistry

logger = logging.getLogger(__name__)


class RuntimeTelemetryEmitter:
    """Publish runtime status/error projections to the compatibility bus."""

    def __init__(
        self,
        persona: AgentPersona,
        roadmap_phase: str,
        goal: str,
        hypothesis: Optional[str] = None,
        required_evidence: Optional[list[str]] = None,
        success_criteria: Optional[list[str]] = None,
        message_bus: Optional[MessageBus] = None,
        status_topic: Topic = TopicRegistry.RUNTIME_STATUS,
        error_topic: Topic = TopicRegistry.RUNTIME_ERROR,
        safety_tier: SafetyTier = SafetyTier.INTERNAL,
        sender_lab: Optional[str] = None,
        agent_name: Optional[str] = None,
        subsystem: str = "runtime",
    ) -> None:
        self.persona = persona
        self.roadmap_phase = roadmap_phase
        self.status_topic = status_topic
        self.error_topic = error_topic
        self.safety_tier = safety_tier
        self.sender_lab = sender_lab
        self.agent_name = agent_name
        self.subsystem = subsystem

        self.intent = ResearchIntent(
            goal=goal,
            hypothesis=hypothesis,
            required_evidence=required_evidence or ["status_events", "error_artifacts"],
            success_criteria=success_criteria or ["telemetry_emitted", "errors_captured"],
        )

        self.message_bus = message_bus or MessageBus(store=MessageStore())

    @staticmethod
    def _normalize_refs(values: Optional[Iterable[Any]]) -> list[str]:
        refs: list[str] = []
        for value in values or []:
            text = str(value or "").strip()
            if text:
                refs.append(text)
        return refs

    def _build_header(
        self,
        *,
        message_type: MessageType,
        session_id: Optional[str],
        run_id: Optional[str],
        program_id: Optional[str],
        agent_name: Optional[str],
        correlation_id: Optional[str],
        parent_correlation_id: Optional[str],
    ) -> MessageHeader:
        return MessageHeader(
            sender_persona=self.persona,
            sender_lab=self.sender_lab,
            message_type=message_type,
            roadmap_phase=self.roadmap_phase,
            safety_tier=self.safety_tier,
            session_id=session_id,
            run_id=run_id,
            program_id=program_id,
            agent_name=agent_name or self.agent_name,
            correlation_id=correlation_id,
            parent_correlation_id=parent_correlation_id,
        )

    def _build_context(
        self,
        *,
        context: Optional[Dict[str, Any]],
        severity: str,
        subsystem: Optional[str],
        artifact_refs: Optional[Iterable[Any]],
        evidence_refs: Optional[Iterable[Any]],
        source_ids: Optional[Iterable[Any]],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload_context = dict(context or {})
        payload_context.setdefault("severity", severity)
        payload_context.setdefault("subsystem", subsystem or self.subsystem)
        refs = self._normalize_refs(artifact_refs)
        if refs:
            payload_context.setdefault("artifact_refs", refs)
        evidence = self._normalize_refs(evidence_refs)
        if evidence:
            payload_context.setdefault("evidence_refs", evidence)
        sources = self._normalize_refs(source_ids)
        if sources:
            payload_context.setdefault("source_ids", sources)
        for key, value in (extra or {}).items():
            if value is not None:
                payload_context.setdefault(key, value)
        return payload_context

    async def emit_status(
        self,
        phase: str,
        status: str,
        progress: Optional[float] = None,
        details: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        severity: str = "info",
        subsystem: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        program_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        correlation_id: Optional[str] = None,
        parent_correlation_id: Optional[str] = None,
        artifact_refs: Optional[Iterable[Any]] = None,
        evidence_refs: Optional[Iterable[Any]] = None,
        source_ids: Optional[Iterable[Any]] = None,
    ) -> None:
        if not self.message_bus:
            return

        payload = StatusPayload(
            phase=phase,
            status=status,
            progress=progress,
            details=details,
            metrics=metrics or {},
        )
        header = self._build_header(
            message_type=MessageType.STATUS,
            session_id=session_id,
            run_id=run_id,
            program_id=program_id,
            agent_name=agent_name,
            correlation_id=correlation_id,
            parent_correlation_id=parent_correlation_id,
        )
        message = AgentMessage(
            header=header,
            intent=self.intent,
            payload=payload,
            context=self._build_context(
                context=context,
                severity=severity,
                subsystem=subsystem,
                artifact_refs=artifact_refs,
                evidence_refs=evidence_refs,
                source_ids=source_ids,
            ),
            artifact_refs=self._normalize_refs(artifact_refs),
            evidence_refs=self._normalize_refs(evidence_refs),
            source_ids=self._normalize_refs(source_ids),
        )

        try:
            await self.message_bus.publish(self.status_topic or TopicRegistry.EXPERIMENT_PROGRESS, message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to emit status telemetry: %s", exc)

    async def emit_error(
        self,
        phase: str,
        error_type: str,
        message: str,
        traceback_text: Optional[str] = None,
        artifact_path: Optional[str] = None,
        rescue_suggestion: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        severity: str = "error",
        subsystem: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        program_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        correlation_id: Optional[str] = None,
        parent_correlation_id: Optional[str] = None,
        artifact_refs: Optional[Iterable[Any]] = None,
        evidence_refs: Optional[Iterable[Any]] = None,
        source_ids: Optional[Iterable[Any]] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        if not self.message_bus:
            return

        payload = ErrorPayload(
            phase=phase,
            error_type=error_type,
            message=message,
            traceback=traceback_text,
            artifact_path=artifact_path,
            rescue_suggestion=rescue_suggestion,
        )
        header = self._build_header(
            message_type=MessageType.ERROR,
            session_id=session_id,
            run_id=run_id,
            program_id=program_id,
            agent_name=agent_name,
            correlation_id=correlation_id,
            parent_correlation_id=parent_correlation_id,
        )
        agent_message = AgentMessage(
            header=header,
            intent=self.intent,
            payload=payload,
            context=self._build_context(
                context=context,
                severity=severity,
                subsystem=subsystem,
                artifact_refs=artifact_refs,
                evidence_refs=evidence_refs,
                source_ids=source_ids,
                extra={"retryable": retryable},
            ),
            artifact_refs=self._normalize_refs(artifact_refs or ([artifact_path] if artifact_path else [])),
            evidence_refs=self._normalize_refs(evidence_refs),
            source_ids=self._normalize_refs(source_ids),
        )

        try:
            await self.message_bus.publish(self.error_topic or TopicRegistry.EXPERIMENT_FAILED, agent_message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to emit error telemetry: %s", exc)


class TelemetryEmitter(RuntimeTelemetryEmitter):
    """Backward-compatible alias for older call sites."""