"""Async specialist debate backbone over the compatibility MessageBus."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence, cast
from uuid import UUID, uuid4

from bsm.communication.message_schema import (
    AgentMessage,
    AgentPersona,
    ErrorPayload,
    MessageHeader,
    MessageType,
    QueryPayload,
    ResearchIntent,
    ResultPayload,
    SafetyTier,
    StatusPayload,
)
from bsm.communication.protocol import MessageBus, MessageStore, Topic, TopicRegistry

DebateMessageType = Literal[
    "specialist.proposal",
    "specialist.critique",
    "specialist.revision",
    "specialist.review",
    "specialist.decision_receipt",
    "specialist.timeout",
    "specialist.dead_letter",
]
DebateVerdict = Literal["accepted", "rejected", "hold"]
TimeoutState = Literal["clear", "timeout", "dead_letter"]
DebateHandler = Callable[[AgentMessage, "SpecialistDebateEnvelope", Dict[str, Any]], Any]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DebateTimeoutPolicy:
    timeout_s: float = 0.0
    dead_letter_topic: str = str(TopicRegistry.SPECIALIST_DEAD_LETTER)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeout_s": self.timeout_s,
            "dead_letter_topic": self.dead_letter_topic,
        }


@dataclass(frozen=True)
class SpecialistDebateEnvelope:
    session_id: str
    run_id: str
    protocol_id: str
    node_id: str
    participant_id: str
    message_type: DebateMessageType
    causal_parent_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_iso)
    timeout_policy: DebateTimeoutPolicy = field(default_factory=DebateTimeoutPolicy)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "protocol_id": self.protocol_id,
            "node_id": self.node_id,
            "participant_id": self.participant_id,
            "message_type": self.message_type,
            "causal_parent_id": self.causal_parent_id,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
            "timeout_policy": self.timeout_policy.to_dict(),
        }

    @classmethod
    def from_message(cls, message: AgentMessage) -> "SpecialistDebateEnvelope":
        raw = message.context.get("specialist_debate")
        if not isinstance(raw, dict):
            raise ValueError("Message does not carry a specialist_debate envelope")
        timeout_policy = raw.get("timeout_policy") or {}
        message_type = cast(DebateMessageType, str(raw["message_type"]))
        return cls(
            session_id=str(raw.get("session_id") or ""),
            run_id=str(raw.get("run_id") or ""),
            protocol_id=str(raw.get("protocol_id") or ""),
            node_id=str(raw.get("node_id") or ""),
            participant_id=str(raw.get("participant_id") or ""),
            message_type=message_type,
            causal_parent_id=str(raw.get("causal_parent_id") or ""),
            payload=dict(raw.get("payload") or {}),
            timestamp=str(raw.get("timestamp") or _utc_iso()),
            timeout_policy=DebateTimeoutPolicy(
                timeout_s=float(timeout_policy.get("timeout_s") or 0.0),
                dead_letter_topic=str(
                    timeout_policy.get("dead_letter_topic")
                    or TopicRegistry.SPECIALIST_DEAD_LETTER
                ),
            ),
        )


@dataclass(frozen=True)
class SpecialistDecisionReceipt:
    verdict: DebateVerdict
    evidence_refs: List[str] = field(default_factory=list)
    unresolved_objections: List[str] = field(default_factory=list)
    participant_ids: List[str] = field(default_factory=list)
    message_ids_consumed: List[str] = field(default_factory=list)
    timeout_state: TimeoutState = "clear"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "evidence_refs": list(self.evidence_refs),
            "unresolved_objections": list(self.unresolved_objections),
            "participant_ids": list(self.participant_ids),
            "message_ids_consumed": list(self.message_ids_consumed),
            "timeout_state": self.timeout_state,
        }


_TOPIC_BY_MESSAGE_TYPE: Dict[DebateMessageType, Topic] = {
    "specialist.proposal": TopicRegistry.SPECIALIST_PROPOSAL,
    "specialist.critique": TopicRegistry.SPECIALIST_CRITIQUE,
    "specialist.revision": TopicRegistry.SPECIALIST_REVISION,
    "specialist.review": TopicRegistry.SPECIALIST_REVIEW,
    "specialist.decision_receipt": TopicRegistry.SPECIALIST_DECISION_RECEIPT,
    "specialist.timeout": TopicRegistry.SPECIALIST_TIMEOUT,
    "specialist.dead_letter": TopicRegistry.SPECIALIST_DEAD_LETTER,
}


class AsyncSpecialistDebateBackbone:
    """Freeze typed debate envelopes over the existing compatibility bus."""

    def __init__(
        self,
        *,
        bus: Optional[MessageBus] = None,
        store: Optional[MessageStore] = None,
        roadmap_phase: str = "MO-03",
        goal: str = "Run async specialist debate over the compatibility MessageBus.",
    ) -> None:
        self._bus = bus or MessageBus(store=store or MessageStore())
        self._roadmap_phase = roadmap_phase
        self._goal = goal
        self._participant_topics: Dict[str, List[Topic]] = {}

    @property
    def bus(self) -> MessageBus:
        return self._bus

    @property
    def store(self) -> MessageStore:
        return self._bus.store

    def subscribe(self, *, participant_id: str, topics: Sequence[Topic | str], handler: DebateHandler) -> None:
        normalized_topics = [Topic(str(topic)) for topic in topics]

        async def _wrapped(message: AgentMessage, context: Dict[str, Any]) -> None:
            try:
                envelope = SpecialistDebateEnvelope.from_message(message)
            except ValueError:
                return
            result = handler(message, envelope, context)
            if inspect.isawaitable(result):
                await result

        _wrapped.__name__ = f"{participant_id}_debate_handler"

        for topic in normalized_topics:
            self._bus.subscribe(topic, _wrapped)
        self._participant_topics.setdefault(participant_id, []).extend(normalized_topics)

    def collect_messages(self, correlation_id: str) -> List[AgentMessage]:
        return [
            message
            for message in self.store.messages
            if message.header.correlation_id == correlation_id
            or message.header.parent_correlation_id == correlation_id
        ]

    def has_participant_message(
        self,
        correlation_id: str,
        participant_id: str,
        *,
        message_types: Optional[Iterable[DebateMessageType]] = None,
    ) -> bool:
        allowed = set(message_types or [])
        for message in self.collect_messages(correlation_id):
            try:
                envelope = SpecialistDebateEnvelope.from_message(message)
            except ValueError:
                continue
            if envelope.participant_id != participant_id:
                continue
            if allowed and envelope.message_type not in allowed:
                continue
            return True
        return False

    def save_store(self, file_path: Path) -> None:
        self._bus.save_store(file_path)

    async def publish(
        self,
        *,
        session_id: str,
        run_id: str,
        protocol_id: str,
        node_id: str,
        participant_id: str,
        message_type: DebateMessageType,
        payload: Dict[str, Any],
        parent_message_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        timeout_s: float = 0.0,
        dead_letter_topic: str = str(TopicRegistry.SPECIALIST_DEAD_LETTER),
        evidence_refs: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
    ) -> UUID:
        correlation = correlation_id or str(uuid4())
        envelope = SpecialistDebateEnvelope(
            session_id=session_id,
            run_id=run_id,
            protocol_id=protocol_id,
            node_id=node_id,
            participant_id=participant_id,
            message_type=message_type,
            causal_parent_id=str(parent_message_id or ""),
            payload=dict(payload),
            timeout_policy=DebateTimeoutPolicy(
                timeout_s=timeout_s,
                dead_letter_topic=dead_letter_topic,
            ),
        )
        message = self._build_agent_message(
            envelope=envelope,
            parent_message_id=parent_message_id,
            correlation_id=correlation,
            evidence_refs=evidence_refs or [],
            source_ids=source_ids or [],
        )
        await self._bus.publish(_TOPIC_BY_MESSAGE_TYPE[message_type], message)
        return message.header.message_id

    async def publish_proposal(self, **kwargs: Any) -> UUID:
        return await self.publish(message_type="specialist.proposal", **kwargs)

    async def publish_critique(self, **kwargs: Any) -> UUID:
        return await self.publish(message_type="specialist.critique", **kwargs)

    async def publish_revision(self, **kwargs: Any) -> UUID:
        return await self.publish(message_type="specialist.revision", **kwargs)

    async def publish_review(self, **kwargs: Any) -> UUID:
        return await self.publish(message_type="specialist.review", **kwargs)

    async def publish_decision_receipt(
        self,
        *,
        receipt: SpecialistDecisionReceipt,
        **kwargs: Any,
    ) -> UUID:
        payload = dict(kwargs.pop("payload", {}) or {})
        payload.update(receipt.to_dict())
        kwargs["payload"] = payload
        return await self.publish(message_type="specialist.decision_receipt", **kwargs)

    async def publish_timeout(self, **kwargs: Any) -> UUID:
        return await self.publish(message_type="specialist.timeout", **kwargs)

    async def publish_dead_letter(self, **kwargs: Any) -> UUID:
        return await self.publish(message_type="specialist.dead_letter", **kwargs)

    async def monitor_timeout(
        self,
        *,
        session_id: str,
        run_id: str,
        protocol_id: str,
        node_id: str,
        parent_message_id: UUID,
        correlation_id: str,
        expected_participant_id: str,
        monitor_participant_id: str = "timeout_monitor",
        timeout_s: float,
        required_message_types: Optional[Sequence[DebateMessageType]] = None,
        reason: str = "Expected participant did not reply before the timeout window elapsed.",
    ) -> Optional[UUID]:
        await asyncio.sleep(timeout_s)
        if self.has_participant_message(
            correlation_id,
            expected_participant_id,
            message_types=required_message_types,
        ):
            return None

        timeout_payload = {
            "expected_participant_id": expected_participant_id,
            "reason": reason,
            "timeout_s": timeout_s,
            "correlation_id": correlation_id,
        }
        timeout_message_id = await self.publish_timeout(
            session_id=session_id,
            run_id=run_id,
            protocol_id=protocol_id,
            node_id=node_id,
            participant_id=monitor_participant_id,
            parent_message_id=parent_message_id,
            correlation_id=correlation_id,
            timeout_s=timeout_s,
            payload=timeout_payload,
            evidence_refs=[f"timeout://{expected_participant_id}"],
        )
        await self.publish_dead_letter(
            session_id=session_id,
            run_id=run_id,
            protocol_id=protocol_id,
            node_id=node_id,
            participant_id=monitor_participant_id,
            parent_message_id=timeout_message_id,
            correlation_id=correlation_id,
            timeout_s=timeout_s,
            payload={
                **timeout_payload,
                "dead_letter_topic": str(TopicRegistry.SPECIALIST_DEAD_LETTER),
            },
            evidence_refs=[f"dead_letter://{expected_participant_id}"],
        )
        return timeout_message_id

    def _build_agent_message(
        self,
        *,
        envelope: SpecialistDebateEnvelope,
        parent_message_id: Optional[UUID],
        correlation_id: str,
        evidence_refs: List[str],
        source_ids: List[str],
    ) -> AgentMessage:
        payload_model, message_type = self._build_payload(envelope, parent_message_id)
        return AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                sender_lab="multiagent_orchestration",
                message_type=message_type,
                roadmap_phase=self._roadmap_phase,
                parent_message_id=parent_message_id,
                safety_tier=SafetyTier.INTERNAL,
                session_id=envelope.session_id,
                run_id=envelope.run_id,
                program_id=envelope.protocol_id or None,
                agent_name=envelope.participant_id,
                correlation_id=correlation_id,
                parent_correlation_id=correlation_id if parent_message_id else None,
            ),
            intent=ResearchIntent(
                goal=f"Exchange {envelope.message_type} over the MessageBus for convergence.",
                hypothesis=str(envelope.payload.get("claim") or envelope.payload.get("verdict") or ""),
                required_evidence=list(envelope.payload.get("required_evidence") or []),
                success_criteria=[
                    "Persist the debate event with causal ancestry and typed envelope metadata.",
                ],
            ),
            payload=payload_model,
            artifact_refs=list(envelope.payload.get("artifact_refs") or []),
            evidence_refs=evidence_refs,
            source_ids=source_ids,
            context={
                "specialist_debate": envelope.to_dict(),
                "specialist_decision_receipt": (
                    dict(envelope.payload)
                    if envelope.message_type == "specialist.decision_receipt"
                    else None
                ),
            },
        )

    def _build_payload(
        self,
        envelope: SpecialistDebateEnvelope,
        parent_message_id: Optional[UUID],
    ) -> tuple[QueryPayload | ResultPayload | StatusPayload | ErrorPayload, MessageType]:
        summary = self._summarize(envelope)
        if envelope.message_type == "specialist.decision_receipt":
            verdict = str(envelope.payload.get("verdict") or "hold")
            success = verdict == "accepted"
            return (
                ResultPayload(
                    experiment_id=parent_message_id or uuid4(),
                    success=success,
                    summary=summary,
                    metrics={
                        "timeout_state": envelope.payload.get("timeout_state") or "clear",
                        "messages_consumed": len(envelope.payload.get("message_ids_consumed") or []),
                    },
                    observations=list(envelope.payload.get("unresolved_objections") or []),
                    errors=None if success else [f"Debate verdict: {verdict}"],
                ),
                MessageType.RESULT,
            )
        if envelope.message_type == "specialist.timeout":
            return (
                StatusPayload(
                    phase="specialist.timeout",
                    status="failed",
                    details=summary,
                    metrics=dict(envelope.payload),
                ),
                MessageType.STATUS,
            )
        if envelope.message_type == "specialist.dead_letter":
            return (
                ErrorPayload(
                    phase="specialist.dead_letter",
                    error_type="specialist_dead_letter",
                    message=summary,
                    rescue_suggestion="Review the timeout evidence and rerun the debate or keep the verdict on hold.",
                ),
                MessageType.ERROR,
            )
        return (
            QueryPayload(
                query=summary,
                parameters=dict(envelope.payload),
                expected_response="Continue the debate on the compatibility MessageBus.",
            ),
            MessageType.QUERY,
        )

    def _summarize(self, envelope: SpecialistDebateEnvelope) -> str:
        payload_summary = str(
            envelope.payload.get("summary")
            or envelope.payload.get("claim")
            or envelope.payload.get("reason")
            or envelope.payload.get("verdict")
            or "structured debate event"
        )
        return (
            f"{envelope.message_type} emitted by {envelope.participant_id} for protocol "
            f"{envelope.protocol_id or envelope.node_id or 'unscoped'}: {payload_summary}"
        )