"""
Communication Protocol and Message Bus for BSM-BUDO-CEA
=======================================================

Implements async message bus and protocol for agent communication.

Features:
- Topic-based pub/sub messaging
- Message persistence and replay
- Schema validation
- Traceability logging
- Safety tier enforcement

Architecture inspired by:
- NaviAgent multi-path routing
- SciToolAgent orchestration
- Production message queue patterns (RabbitMQ, Kafka)
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .message_schema import (
    AgentMessage,
    AgentPersona,
    Attachment,
    ErrorPayload,
    MessageHeader,
    MessageType,
    ResearchIntent,
    ResultPayload,
    ReviewDecision,
    SafetyTier,
    ValidationPayload,
)
from .lab_report import LabReport

logger = logging.getLogger(__name__)


class Topic(str):
    """
    Message topic for routing
    
    Topic pattern: {domain}.{action}
    Examples:
        - research.proposal
        - experiment.run
        - experiment.complete
        - analysis.report
        - validation.request
        - validation.feedback
        - error.critical
    """
    pass


class TopicRegistry:
    """
    Central registry of communication topics
    
    Follows AI University workflow phases
    """
    # Research planning
    RESEARCH_PROPOSAL = Topic("research.proposal")
    RESEARCH_APPROVED = Topic("research.approved")
    RESEARCH_REJECTED = Topic("research.rejected")
    
    # Experiment execution
    EXPERIMENT_REQUEST = Topic("experiment.request")
    EXPERIMENT_START = Topic("experiment.start")
    EXPERIMENT_PROGRESS = Topic("experiment.progress")
    EXPERIMENT_COMPLETE = Topic("experiment.complete")
    EXPERIMENT_FAILED = Topic("experiment.failed")
    
    # Analysis and reporting
    ANALYSIS_REQUEST = Topic("analysis.request")
    ANALYSIS_COMPLETE = Topic("analysis.complete")
    REPORT_GENERATED = Topic("report.generated")
    
    # Validation and review
    VALIDATION_REQUEST = Topic("validation.request")
    VALIDATION_FEEDBACK = Topic("validation.feedback")
    PEER_REVIEW = Topic("peer.review")
    RUNTIME_STATUS = Topic("telemetry.runtime.status")
    RUNTIME_ERROR = Topic("telemetry.runtime.error")
    REVIEW_STATUS = Topic("telemetry.review.status")
    EVIDENCE_STATUS = Topic("telemetry.evidence.status")
    SPECIALIST_STATUS = Topic("telemetry.specialist.status")
    RUNTIME_REVIEW = Topic("runtime.review")
    RUNTIME_ARTIFACT = Topic("runtime.artifact")
    RUNTIME_SHARED_KERNEL = Topic("runtime.shared_kernel.snapshot")
    RUNTIME_PROTOCOL_RUN = Topic("runtime.protocol.run")
    RUNTIME_PROTOCOL_NODE = Topic("runtime.protocol.node")
    RUNTIME_PROTOCOL_UNIFIED = Topic("runtime.protocol.unified")

    # Async specialist debate
    SPECIALIST_PROPOSAL = Topic("specialist.proposal")
    SPECIALIST_CRITIQUE = Topic("specialist.critique")
    SPECIALIST_REVISION = Topic("specialist.revision")
    SPECIALIST_REVIEW = Topic("specialist.review")
    SPECIALIST_DECISION_RECEIPT = Topic("specialist.decision_receipt")
    SPECIALIST_TIMEOUT = Topic("specialist.timeout")
    SPECIALIST_DEAD_LETTER = Topic("specialist.dead_letter")
    
    # Knowledge sharing
    KNOWLEDGE_SHARE = Topic("knowledge.share")
    KNOWLEDGE_QUERY = Topic("knowledge.query")
    
    # System events
    ERROR_CRITICAL = Topic("error.critical")
    ERROR_WARNING = Topic("error.warning")
    STATUS_UPDATE = Topic("status.update")
    
    @classmethod
    def all_topics(cls) -> List[Topic]:
        """Get all registered topics"""
        return [
            getattr(cls, attr) 
            for attr in dir(cls) 
            if not attr.startswith('_') and isinstance(getattr(cls, attr), Topic)
        ]


class MessageHandler:
    """
    Base class for message handlers
    """
    async def handle(self, message: AgentMessage, context: Dict[str, Any]) -> None:
        """
        Handle incoming message
        
        Args:
            message: The agent message to handle
            context: Additional context (bus ref, etc.)
        """
        raise NotImplementedError


class MessageStore(BaseModel):
    """
    Persistent message storage for replay and traceability
    """
    messages: List[AgentMessage] = Field(default_factory=list, description="Stored messages")
    message_index: Dict[str, UUID] = Field(default_factory=dict, description="Message ID index")
    
    def add(self, message: AgentMessage) -> None:
        """Add message to store"""
        self.messages.append(message)
        self.message_index[str(message.header.message_id)] = message.header.message_id
    
    def get(self, message_id: UUID) -> Optional[AgentMessage]:
        """Retrieve message by ID"""
        indexed = self.message_index.get(str(message_id))
        if indexed is None:
            return None
        for msg in self.messages:
            if msg.header.message_id == indexed:
                return msg
        return None
    
    def get_by_persona(self, persona: AgentPersona) -> List[AgentMessage]:
        """Get all messages from a specific persona"""
        return [msg for msg in self.messages if msg.header.sender_persona == persona]
    
    def get_by_phase(self, phase: str) -> List[AgentMessage]:
        """Get all messages for a specific roadmap phase"""
        return [msg for msg in self.messages if msg.header.roadmap_phase == phase]
    
    def get_thread(self, root_message_id: UUID) -> List[AgentMessage]:
        """Get all messages in a thread"""
        thread = []
        
        # BFS to find all descendants
        queue = [root_message_id]
        visited = set()
        
        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)
            
            # Find message
            msg = self.get(current_id)
            if msg:
                thread.append(msg)
                
                # Find children
                children = [
                    m for m in self.messages 
                    if m.header.parent_message_id == current_id
                ]
                queue.extend([c.header.message_id for c in children])
        
        return thread
    
    def save_to_file(self, file_path: Path) -> None:
        """Save message store to JSON file"""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(self.model_dump_json(indent=2))
        logger.info(f"Saved {len(self.messages)} messages to {file_path}")
    
    @classmethod
    def load_from_file(cls, file_path: Path) -> 'MessageStore':
        """Load message store from JSON file"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = f.read()
        store = cls.model_validate_json(data)
        logger.info(f"Loaded {len(store.messages)} messages from {file_path}")
        return store


class MessageBus:
    """
    Async message bus for agent communication
    
    Provides:
    - Topic-based pub/sub
    - Message validation
    - Handler registration
    - Persistence
    - Traceability logging
    """
    
    def __init__(self, 
                 store: Optional[MessageStore] = None,
                 bitacora_dir: Optional[Path] = None,
                 enforce_safety: bool = True):
        """
        Initialize message bus
        
        Args:
            store: Optional message store for persistence
            bitacora_dir: Directory for Bitácora logging
            enforce_safety: Whether to enforce safety tier restrictions
        """
        self.store = store or MessageStore()
        self.bitacora_dir = bitacora_dir
        self.enforce_safety = enforce_safety
        
        # Subscribers: topic -> list of handlers
        self.subscribers: Dict[Topic, List[Callable]] = defaultdict(list)
        
        # Active subscriptions per persona (for cleanup)
        self.persona_subscriptions: Dict[AgentPersona, Set[Topic]] = defaultdict(set)
        
        logger.info(f"MessageBus initialized (safety={enforce_safety})")
    
    def subscribe(self, 
                  topic: Topic, 
                  handler: Callable[[AgentMessage, Dict[str, Any]], None],
                  persona: Optional[AgentPersona] = None) -> None:
        """
        Subscribe to a topic
        
        Args:
            topic: Topic to subscribe to
            handler: Async function to handle messages
            persona: Optional persona for tracking subscriptions
        """
        self.subscribers[topic].append(handler)
        if persona:
            self.persona_subscriptions[persona].add(topic)
        logger.info(f"Subscribed to {topic} (handler={handler.__name__}, persona={persona})")
    
    def unsubscribe(self, topic: Topic, handler: Callable) -> None:
        """Unsubscribe handler from topic"""
        if topic in self.subscribers:
            self.subscribers[topic].remove(handler)
            logger.info(f"Unsubscribed from {topic} (handler={handler.__name__})")
    
    async def publish(self, 
                     topic: Topic, 
                     message: AgentMessage,
                     context: Optional[Dict[str, Any]] = None) -> None:
        """
        Publish message to topic
        
        Args:
            topic: Topic to publish to
            message: Agent message to publish
            context: Optional additional context
        """
        context = context or {}
        context['bus'] = self
        context['topic'] = topic
        context['publish_time'] = datetime.now(timezone.utc)
        
        # Validate message
        if not context.get("skip_validation"):
            try:
                # Pydantic validates on construction, but re-validate to be safe
                AgentMessage.model_validate(message.model_dump())
            except Exception as e:
                logger.error(f"Message validation failed: {e}")
                await self._publish_error(topic, message, e)
                raise
        
        # Enforce safety tier if enabled
        if self.enforce_safety:
            await self._check_safety_tier(message)
        
        # Store message
        self.store.add(message)
        
        # Log to Bitácora if configured
        if self.bitacora_dir:
            await self._log_to_bitacora(message)
        
        # Notify subscribers
        handlers = self.subscribers.get(topic, [])
        if not handlers:
            logger.warning(f"No subscribers for topic: {topic}")
        
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(message, context)
                else:
                    handler(message, context)
            except Exception as e:
                logger.error(f"Handler {handler.__name__} failed for {topic}: {e}")
                await self._publish_error(topic, message, e)
        
        logger.info(
            f"Published to {topic}: {message.header.message_type} "
            f"from {message.header.sender_persona.value} ({len(handlers)} handlers)"
        )
    
    async def _check_safety_tier(self, message: AgentMessage) -> None:
        """
        Enforce safety tier restrictions
        
        Inspired by SciToolAgent SEGURIDAD_MCP.md
        """
        tier = message.header.safety_tier
        
        if tier == SafetyTier.CONFIDENTIAL:
            # Check if sender is authorized for confidential data
            authorized_personas = {
                AgentPersona.ALEX_RODRIGUEZ,  # System architect
                AgentPersona.DR_SOFIA_PETROV,  # Infrastructure lead
            }
            if message.header.sender_persona not in authorized_personas:
                raise PermissionError(
                    f"Persona {message.header.sender_persona.value} not authorized "
                    f"for CONFIDENTIAL tier"
                )
        
        # Log safety tier usage
        logger.info(f"Safety check passed: {tier.value}")
    
    async def _log_to_bitacora(self, message: AgentMessage) -> None:
        """
        Log message to appropriate Bitácora file
        
        Follows AI University DAILY_LOGS format
        """
        if not self.bitacora_dir:
            return
        
        # Determine log file based on sender persona
        persona_lab = {
            AgentPersona.DR_YUAN_CHEN: "yuan_chen",
            AgentPersona.DR_SOFIA_PETROV: "sofia_petrov",
            AgentPersona.DR_PRIYA_SHARMA: "priya_sharma",
            AgentPersona.ALEX_RODRIGUEZ: "alex_rodriguez",
            AgentPersona.DR_ARIS_THORNE: "aris_thorne",
        }
        
        lab_name = persona_lab.get(message.header.sender_persona, "system")
        log_dir = self.bitacora_dir / lab_name / "DAILY_LOGS"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Log file named by date
        log_file = log_dir / f"{message.header.timestamp.strftime('%Y_%m_%d')}_MessageBus.md"
        
        # Append message entry
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\n\n---\n\n")
            f.write(message.to_bitacora_entry())
        
        logger.debug(f"Logged message to {log_file}")
    
    async def _publish_error(self, 
                            topic: Topic, 
                            failed_message: AgentMessage,
                            error: Exception) -> None:
        """
        Publish error notification to ERROR_CRITICAL topic
        """
        error_message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.ERROR,
                roadmap_phase="0.0",
                parent_message_id=failed_message.header.message_id,
                safety_tier=SafetyTier.INTERNAL
            ),
            intent=ResearchIntent(
                goal=f"Report error in message processing",
                required_evidence=[],
                success_criteria=[]
            ),
            payload=ErrorPayload(
                phase=str(topic),
                error_type=type(error).__name__,
                message=str(error),
                rescue_suggestion="Inspect the failed message payload and runtime projection adapter.",
            ),
            attachments=[],
            context={
                "failed_topic": str(topic),
                "failed_message_id": str(failed_message.header.message_id),
                "original_message": failed_message.model_dump(),
            }
        )
        
        # Publish to error topic (without recursive error handling)
        await self.publish(TopicRegistry.ERROR_CRITICAL, error_message, context={'skip_validation': True})
    
    def get_message(self, message_id: UUID) -> Optional[AgentMessage]:
        """Retrieve message by ID"""
        return self.store.get(message_id)
    
    def get_thread(self, root_message_id: UUID) -> List[AgentMessage]:
        """Get message thread"""
        return self.store.get_thread(root_message_id)
    
    def save_store(self, file_path: Path) -> None:
        """Save message store to file"""
        self.store.save_to_file(file_path)
    
    @classmethod
    def load_store(cls, 
                   file_path: Path,
                   bitacora_dir: Optional[Path] = None,
                   enforce_safety: bool = True) -> 'MessageBus':
        """Load message bus with stored messages"""
        store = MessageStore.load_from_file(file_path)
        return cls(store=store, bitacora_dir=bitacora_dir, enforce_safety=enforce_safety)


class CommunicationProtocol:
    """
    High-level protocol for structured agent communication workflows
    
    Provides common patterns:
    - Proposal → Review → Approval workflow
    - Experiment request → Execution → Reporting
    - Validation request → Feedback loop
    """
    
    def __init__(self, bus: MessageBus):
        self.bus = bus
    
    async def propose_experiment(self, 
                                proposal: AgentMessage,
                                reviewers: List[AgentPersona]) -> None:
        """
        Initiate experiment proposal workflow
        
        Workflow:
        1. Publish proposal to RESEARCH_PROPOSAL
        2. Request reviews from specified personas
        3. Collect feedback on VALIDATION_FEEDBACK
        """
        # Publish proposal
        await self.bus.publish(TopicRegistry.RESEARCH_PROPOSAL, proposal)
        
        # Request reviews
        from .message_schema import MessageHeader, ResearchIntent, ValidationPayload
        
        for reviewer in reviewers:
            review_request = AgentMessage(
                header=MessageHeader(
                    sender_persona=proposal.header.sender_persona,
                    message_type=MessageType.VALIDATION,
                    roadmap_phase=proposal.header.roadmap_phase,
                    parent_message_id=proposal.header.message_id,
                    safety_tier=proposal.header.safety_tier
                ),
                intent=ResearchIntent(
                    goal=f"Request review of proposal from {reviewer.value}",
                    required_evidence=["Reviewer feedback"],
                    success_criteria=["Review completed"]
                ),
                payload=ValidationPayload(
                    target_message_id=proposal.header.message_id,
                    validation_type="proposal_review",
                    approved=False,  # Pending
                    feedback="Review requested"
                ),
                attachments=[],
                context={"reviewer": reviewer.value}
            )
            
            await self.bus.publish(TopicRegistry.VALIDATION_REQUEST, review_request)
    
    async def submit_lab_report(self, 
                                report: LabReport,
                                sender_persona: AgentPersona) -> UUID:
        """
        Submit lab report and generate announcement message
        
        Returns:
            Message ID of report announcement
        """
        # Save report to lab directory
        report_path = report.save_to_lab(
            report_dir=str(Path(report.metadata.lab_directory) / "REPORTS"),
            format="markdown"
        )
        
        # Create announcement message
        announcement = AgentMessage(
            header=MessageHeader(
                sender_persona=sender_persona,
                message_type=MessageType.RESULT,
                roadmap_phase=report.metadata.roadmap_phase,
                safety_tier=SafetyTier.INTERNAL
            ),
            intent=ResearchIntent(
                goal=f"Report results of {report.metadata.title}",
                required_evidence=[],
                success_criteria=[]
            ),
            payload=ResultPayload(
                experiment_id=report.metadata.experiment_id,
                success=True,
                summary=report.abstract,
                data_artifacts=[],
                metrics={"report_type": "lab_report", "title": report.metadata.title},
                observations=["Lab report exported and announced through CommunicationProtocol."],
            ),
            attachments=[
                Attachment(
                    file_path=report_path,
                    file_type="text/markdown",
                    description="Complete lab report",
                    size_bytes=None,
                    checksum=None
                )
            ],
            context={"report_saved": report_path}
        )
        
        await self.bus.publish(TopicRegistry.REPORT_GENERATED, announcement)
        
        return announcement.header.message_id

    async def publish_review_projection(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_name: str,
        focus: str,
        critique: str,
        verdict: Dict[str, Any],
        review_issues: List[Dict[str, Any]],
        peer_feedback: Optional[Dict[str, Any]] = None,
        quality_score: Optional[Dict[str, Any]] = None,
        artifact_path: Optional[str] = None,
    ) -> UUID:
        feedback_text = critique[:4000] if critique else "Peer review projection generated with actionable findings."
        if len(feedback_text) < 20:
            feedback_text = f"{feedback_text} Additional review details were captured for runtime projection."
        decision_map = {
            "ACCEPT": ReviewDecision.ACCEPT,
            "MINOR_REVISION": ReviewDecision.MINOR_REVISION,
            "MAJOR_REVISION": ReviewDecision.MAJOR_REVISION,
            "REJECT": ReviewDecision.REJECT,
        }
        required_changes = [
            str(issue.get("recommendation") or issue.get("issue") or "").strip()
            for issue in review_issues
            if str(issue.get("recommendation") or issue.get("issue") or "").strip()
        ]
        source_ids = [
            str(source_id)
            for source_id in verdict.get("source_ids", [])
            if str(source_id).strip()
        ]
        message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.VALIDATION,
                roadmap_phase="runtime.review",
                safety_tier=SafetyTier.INTERNAL,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                correlation_id=str(uuid4()),
            ),
            intent=ResearchIntent(
                goal=f"Persist peer-review projection for {focus}",
                required_evidence=["review_verdict", "review_issues"],
                success_criteria=["review_projection_persisted"],
            ),
            payload=ValidationPayload(
                target_message_id=uuid4(),
                validation_type="peer_review_projection",
                approved=verdict.get("decision") == "ACCEPT",
                decision=decision_map.get(str(verdict.get("decision") or "")),
                feedback=feedback_text,
                required_changes=required_changes,
                reviewer_notes=(
                    "Auto-revision requested." if verdict.get("should_revise") else "No auto-revision required."
                ),
            ),
            attachments=(
                [
                    Attachment(
                        file_path=artifact_path,
                        file_type="text/markdown",
                        description="Peer-review artifact",
                    )
                ]
                if artifact_path
                else []
            ),
            artifact_refs=[artifact_path] if artifact_path else [],
            evidence_refs=[
                str(verdict.get("decision") or "UNKNOWN"),
                *[f"issue:{index}" for index, _ in enumerate(review_issues, start=1)],
            ],
            source_ids=source_ids,
            context={
                "focus": focus,
                "verdict": verdict,
                "review_issues": review_issues,
                "peer_feedback": peer_feedback or {},
                "quality_score": quality_score or {},
            },
        )
        await self.bus.publish(TopicRegistry.RUNTIME_REVIEW, message)
        return message.header.message_id

    async def publish_artifact_announcement(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_name: str,
        summary: str,
        artifact_path: str,
        source_ids: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> UUID:
        result_summary = summary[:4000] if summary else "Artifact announcement generated for persisted runtime output."
        if len(result_summary) < 20:
            result_summary = f"{result_summary} Additional artifact metadata has been persisted for replay."
        message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.RESULT,
                roadmap_phase="runtime.artifact",
                safety_tier=SafetyTier.INTERNAL,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                correlation_id=str(uuid4()),
            ),
            intent=ResearchIntent(
                goal=f"Announce persisted artifact for session {session_id}",
                required_evidence=["artifact_path"],
                success_criteria=["artifact_announcement_persisted"],
            ),
            payload=ResultPayload(
                experiment_id=uuid4(),
                success=True,
                summary=result_summary,
                data_artifacts=[],
                metrics={"artifact_path": artifact_path},
                observations=["Artifact persisted by modern MICA runtime."],
            ),
            attachments=[
                Attachment(
                    file_path=artifact_path,
                    file_type="text/markdown",
                    description="Persisted MICA artifact",
                )
            ],
            artifact_refs=[artifact_path],
            evidence_refs=list(evidence_refs or []),
            source_ids=list(source_ids or []),
            context={"artifact_path": artifact_path},
        )
        await self.bus.publish(TopicRegistry.RUNTIME_ARTIFACT, message)
        return message.header.message_id

    async def publish_protocol_node_receipt(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_name: str,
        receipt: Any,
    ) -> UUID:
        receipt_data = receipt.model_dump(mode="json") if hasattr(receipt, "model_dump") else dict(receipt or {})
        node_id = str(receipt_data.get("node_id") or "unknown-node")
        actor_surface = str(receipt_data.get("actor_surface") or "protocol_executor")
        summary = f"Protocol node receipt persisted for {node_id} on {actor_surface}."
        message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.RESULT,
                roadmap_phase="runtime.protocol.node",
                safety_tier=SafetyTier.INTERNAL,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                correlation_id=str(uuid4()),
            ),
            intent=ResearchIntent(
                goal=f"Persist protocol node receipt for {node_id}",
                required_evidence=["protocol_node_receipt"],
                success_criteria=["protocol_node_receipt_persisted"],
            ),
            payload=ResultPayload(
                experiment_id=uuid4(),
                success=True,
                summary=summary,
                data_artifacts=[],
                metrics={
                    "protocol_id": str(receipt_data.get("protocol_id") or ""),
                    "node_id": node_id,
                    "actor_surface": actor_surface,
                    "actor_id": str(receipt_data.get("actor_id") or ""),
                    "event_type": str(receipt_data.get("event_type") or ""),
                },
                observations=["Protocol node receipt projected through CommunicationProtocol."],
            ),
            artifact_refs=list(receipt_data.get("artifact_refs") or []),
            evidence_refs=list(receipt_data.get("evidence_refs") or []),
            context={"protocol_node_receipt": receipt_data},
        )
        await self.bus.publish(TopicRegistry.RUNTIME_PROTOCOL_NODE, message)
        return message.header.message_id

    async def publish_protocol_run_receipt(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_name: str,
        receipt: Any,
    ) -> UUID:
        receipt_data = receipt.model_dump(mode="json") if hasattr(receipt, "model_dump") else dict(receipt or {})
        protocol_id = str(receipt_data.get("protocol_id") or "unknown-protocol")
        executed_node_ids = list(receipt_data.get("executed_node_ids") or [])
        failed = str(receipt_data.get("status") or "") == "failed"
        summary = (
            f"Protocol run receipt persisted for {protocol_id} after executing {len(executed_node_ids)} node(s)."
        )
        message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.RESULT,
                roadmap_phase="runtime.protocol.run",
                safety_tier=SafetyTier.INTERNAL,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                correlation_id=str(uuid4()),
            ),
            intent=ResearchIntent(
                goal=f"Persist protocol run receipt for {protocol_id}",
                required_evidence=["protocol_run_receipt"],
                success_criteria=["protocol_run_receipt_persisted"],
            ),
            payload=ResultPayload(
                experiment_id=uuid4(),
                success=not failed,
                summary=summary,
                data_artifacts=[],
                metrics={
                    "protocol_id": protocol_id,
                    "status": str(receipt_data.get("status") or ""),
                    "executed_node_count": len(executed_node_ids),
                },
                observations=["Protocol run receipt projected through CommunicationProtocol."],
                errors=["Protocol run ended in failed state."] if failed else None,
            ),
            artifact_refs=list(receipt_data.get("artifact_refs") or []),
            evidence_refs=list(receipt_data.get("evidence_refs") or []),
            context={"protocol_run_receipt": receipt_data},
        )
        await self.bus.publish(TopicRegistry.RUNTIME_PROTOCOL_RUN, message)
        return message.header.message_id

    async def publish_shared_kernel_snapshot(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_name: str,
        snapshot: Dict[str, Any],
    ) -> UUID:
        snapshot_id = str(snapshot.get("snapshot_id") or "unknown-snapshot")
        summary = f"Shared-kernel snapshot bridged onto the compatibility bus for {snapshot_id}."
        message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.RESULT,
                roadmap_phase="runtime.shared_kernel.snapshot",
                safety_tier=SafetyTier.INTERNAL,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                correlation_id=str(uuid4()),
            ),
            intent=ResearchIntent(
                goal=f"Persist shared-kernel snapshot bridge event for {snapshot_id}",
                required_evidence=["shared_kernel_snapshot"],
                success_criteria=["shared_kernel_snapshot_persisted"],
            ),
            payload=ResultPayload(
                experiment_id=uuid4(),
                success=True,
                summary=summary,
                data_artifacts=[],
                metrics={
                    "snapshot_id": snapshot_id,
                    "user_id": str(snapshot.get("user_id") or "default"),
                    "mu": float(snapshot.get("mu") or 0.0),
                    "sigma": float(snapshot.get("sigma") or 0.0),
                    "contributing_quintuples": int(snapshot.get("contributing_quintuples") or 0),
                },
                observations=["Internal EventBus snapshot bridged onto CommunicationProtocol."],
            ),
            artifact_refs=[],
            evidence_refs=[f"event://snapshot/{snapshot_id}"],
            context={"shared_kernel_snapshot": dict(snapshot)},
        )
        await self.bus.publish(TopicRegistry.RUNTIME_SHARED_KERNEL, message)
        return message.header.message_id

    async def publish_unified_protocol_runtime(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_name: str,
        unified_runtime: Dict[str, Any],
    ) -> UUID:
        runtime_payload = dict(unified_runtime or {})
        protocol_id = str(runtime_payload.get("protocol_id") or "unknown-protocol")
        graph_run_status = str(runtime_payload.get("graph_run_status") or "unknown")
        projection_only = bool(runtime_payload.get("projection_only", False))
        summary = (
            f"Unified protocol runtime projection persisted for {protocol_id} "
            f"with graph status {graph_run_status}."
        )
        message = AgentMessage(
            header=MessageHeader(
                sender_persona=AgentPersona.SYSTEM,
                message_type=MessageType.RESULT,
                roadmap_phase="runtime.protocol.unified",
                safety_tier=SafetyTier.INTERNAL,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                correlation_id=str(uuid4()),
            ),
            intent=ResearchIntent(
                goal=f"Persist unified protocol runtime projection for {protocol_id}",
                required_evidence=["unified_protocol_runtime"],
                success_criteria=["unified_protocol_runtime_persisted"],
            ),
            payload=ResultPayload(
                experiment_id=uuid4(),
                success=True,
                summary=summary,
                data_artifacts=[],
                metrics={
                    "protocol_id": protocol_id,
                    "graph_run_status": graph_run_status,
                    "projection_only": projection_only,
                    "node_receipt_count": len(list(runtime_payload.get("node_receipts") or [])),
                    "run_receipt_count": len(list(runtime_payload.get("run_receipts") or [])),
                },
                observations=["Unified protocol runtime projected through CommunicationProtocol."],
            ),
            artifact_refs=list(runtime_payload.get("artifact_refs") or []),
            evidence_refs=list(runtime_payload.get("evidence_refs") or []),
            context={"unified_protocol_runtime": runtime_payload},
        )
        await self.bus.publish(TopicRegistry.RUNTIME_PROTOCOL_UNIFIED, message)
        return message.header.message_id
