"""
Agent Message Schema for BSM-BUDO-CEA
======================================

Pydantic schemas for structured inter-agent communication following:
- AI University workflow standards
- SciToolAgent KG-driven orchestration patterns
- NaviAgent multi-path decision architecture
- Scientific reproducibility requirements

References:
- Perplexity Research: Pydantic validation best practices for YAML configs
- arXiv papers on goal-oriented agent communication
- BSM_BUDO_CEA_UNIFIED_MASTER_ROADMAP.md Phase 0/1/2 requirements
"""

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Union, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SafetyTier(str, Enum):
    """
    Safety tiers inspired by SciToolAgent SEGURIDAD_MCP.md
    """
    PUBLIC = "public"  # Safe for public datasets
    INTERNAL = "internal"  # BSM internal use only
    CONFIDENTIAL = "confidential"  # Restricted access
    EXPERIMENTAL = "experimental"  # Research-only, not validated


class AgentPersona(str, Enum):
    """
    AI University research personas from EXPERT_PERSONAS.md
    """
    DR_YUAN_CHEN = "dr_yuan_chen"
    DR_SOFIA_PETROV = "dr_sofia_petrov"
    DR_PRIYA_SHARMA = "dr_priya_sharma"
    ALEX_RODRIGUEZ = "alex_rodriguez"
    DR_ARIS_THORNE = "dr_aris_thorne"
    SYSTEM = "system"  # For infrastructure messages


class MessageType(str, Enum):
    """
    Message types for different communication patterns
    """
    PROPOSAL = "proposal"  # Research proposal or hypothesis
    EXPERIMENT = "experiment"  # Experiment execution request
    RESULT = "result"  # Experimental results
    VALIDATION = "validation"  # Validation or review feedback
    QUERY = "query"  # Information request
    ERROR = "error"  # Error notification
    STATUS = "status"  # Status update


class ReviewDecision(str, Enum):
    """Review lifecycle outcomes compatible with modern MICA review loops."""

    PENDING = "pending"
    ACCEPT = "accept"
    MINOR_REVISION = "minor_revision"
    MAJOR_REVISION = "major_revision"
    REJECT = "reject"
    BLOCKED = "blocked"


class MessageHeader(BaseModel):
    """
    Message header with routing and traceability metadata
    """
    message_id: UUID = Field(default_factory=uuid4, description="Unique message identifier")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Message creation time")
    sender_persona: AgentPersona = Field(..., description="Sending agent persona")
    sender_lab: Optional[str] = Field(None, description="Lab directory path")
    message_type: MessageType = Field(..., description="Type of message")
    roadmap_phase: str = Field(..., description="BSM-BUDO-CEA phase (e.g., '2.004', '3.5')")
    parent_message_id: Optional[UUID] = Field(None, description="Parent message for threads")
    safety_tier: SafetyTier = Field(default=SafetyTier.INTERNAL, description="Data sensitivity level")
    session_id: Optional[str] = Field(None, description="Runtime session identifier")
    run_id: Optional[str] = Field(None, description="Runtime run identifier")
    program_id: Optional[str] = Field(None, description="Program envelope identifier")
    agent_name: Optional[str] = Field(None, description="Runtime agent or subsystem name")
    correlation_id: Optional[str] = Field(None, description="Cross-envelope correlation identifier")
    parent_correlation_id: Optional[str] = Field(None, description="Parent correlation identifier")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message_id": "550e8400-e29b-41d4-a716-446655440000",
                "timestamp": "2025-10-16T10:30:00Z",
                "sender_persona": "dr_yuan_chen",
                "sender_lab": "labs/yuan_chen",
                "message_type": "experiment",
                "roadmap_phase": "4.002",
                "parent_message_id": None,
                "safety_tier": "internal",
            }
        }
    )


class ResearchIntent(BaseModel):
    """
    Research goal and hypothesis for the message
    """
    goal: str = Field(..., min_length=10, description="Research objective")
    hypothesis: Optional[str] = Field(None, description="Scientific hypothesis being tested")
    required_evidence: List[str] = Field(default_factory=list, description="Evidence needed to validate")
    success_criteria: List[str] = Field(default_factory=list, description="Measurable success criteria")
    
    @field_validator('goal')
    @classmethod
    def validate_goal_clarity(cls, v: str) -> str:
        """Ensure goal is clear and actionable"""
        if len(v.split()) < 5:
            raise ValueError("Goal must be at least 5 words for clarity")
        return v


class Attachment(BaseModel):
    """
    File attachment reference with metadata
    """
    file_path: str = Field(..., description="Absolute path or URI to file")
    file_type: str = Field(..., description="MIME type or file extension")
    description: str = Field(..., description="What the file contains")
    size_bytes: Optional[int] = Field(None, description="File size in bytes")
    checksum: Optional[str] = Field(None, description="SHA256 checksum for integrity")
    
    @field_validator('file_path')
    @classmethod
    def validate_path_format(cls, v: str) -> str:
        """Ensure path is absolute or valid URI"""
        if not (
            v.startswith('/')
            or v.startswith('http://')
            or v.startswith('https://')
            or v.startswith('artifact://')
            or v.startswith('\\\\')
            or v.startswith('./')
            or v.startswith('../')
            or re.match(r'^[A-Za-z]:\\', v)
        ):
            raise ValueError(f"File path must be absolute or URI: {v}")
        return v


class Citation(BaseModel):
    """
    Scientific citation for external knowledge
    """
    source: str = Field(..., description="Source (DOI, arXiv ID, URL)")
    title: str = Field(..., description="Paper or resource title")
    authors: Optional[List[str]] = Field(None, description="Author list")
    year: Optional[int] = Field(None, description="Publication year")
    relevant_finding: str = Field(..., description="Why this citation is relevant")


class ProposalPayload(BaseModel):
    """
    Research proposal payload for PROPOSAL messages
    """
    payload_type: Literal["proposal"] = Field("proposal", description="Payload discriminator")
    title: str = Field(..., min_length=10, description="Proposal title")
    background: str = Field(..., min_length=50, description="Background and motivation")
    methods: str = Field(..., min_length=50, description="Proposed methodology")
    expected_outcomes: List[str] = Field(..., min_length=1, description="Expected deliverables")
    resources_needed: Dict[str, Any] = Field(default_factory=dict, description="Required resources")
    collaborators: List[AgentPersona] = Field(default_factory=list, description="Other personas needed")
    citations: List[Citation] = Field(default_factory=list, description="Supporting literature")


class ExperimentPayload(BaseModel):
    """
    Experiment execution request for EXPERIMENT messages
    """
    payload_type: Literal["experiment"] = Field("experiment", description="Payload discriminator")
    experiment_name: str = Field(..., description="Descriptive experiment name")
    protocol: str = Field(..., description="Detailed experimental protocol")
    parameters: Dict[str, Any] = Field(..., description="Experiment parameters")
    expected_duration: Optional[str] = Field(None, description="Estimated time (e.g., '2 hours', '3 days')")
    output_formats: List[str] = Field(default_factory=list, description="Desired output formats")
    validation_checks: List[str] = Field(default_factory=list, description="Quality checks to perform")


class ResultPayload(BaseModel):
    """
    Experimental results for RESULT messages
    """
    payload_type: Literal["result"] = Field("result", description="Payload discriminator")
    experiment_id: UUID = Field(..., description="Reference to experiment message")
    success: bool = Field(..., description="Whether experiment succeeded")
    summary: str = Field(..., min_length=20, description="Result summary")
    data_artifacts: List[Attachment] = Field(default_factory=list, description="Generated data files")
    metrics: Dict[str, Union[float, int, str]] = Field(default_factory=dict, description="Quantitative metrics")
    observations: List[str] = Field(default_factory=list, description="Qualitative observations")
    errors: Optional[List[str]] = Field(None, description="Error messages if failed")
    
    @model_validator(mode='after')
    def validate_success_consistency(self):
        """Ensure success flag matches presence of errors"""
        if not self.success and not self.errors:
            raise ValueError("Failed experiments must include error descriptions")
        if self.success and self.errors:
            raise ValueError("Successful experiments should not have errors")
        return self


class ValidationPayload(BaseModel):
    """
    Validation feedback for VALIDATION messages
    """
    payload_type: Literal["validation"] = Field("validation", description="Payload discriminator")
    target_message_id: UUID = Field(..., description="Message being validated")
    validation_type: str = Field(..., description="Type of validation (peer_review, qa_check, etc.)")
    approved: bool = Field(..., description="Whether validation passed")
    decision: Optional[ReviewDecision] = Field(None, description="Structured review decision")
    feedback: str = Field(..., min_length=20, description="Detailed feedback")
    required_changes: List[str] = Field(default_factory=list, description="Changes needed if not approved")
    reviewer_notes: Optional[str] = Field(None, description="Additional reviewer notes")


class StatusPayload(BaseModel):
    """Status update payload for STATUS messages"""

    payload_type: Literal["status"] = Field("status", description="Payload discriminator")
    phase: str = Field(..., description="Lifecycle phase identifier")
    status: Literal["started", "in_progress", "completed", "failed"] = Field(..., description="Status flag")
    progress: Optional[float] = Field(None, ge=0.0, le=1.0, description="Fractional progress 0-1")
    details: Optional[str] = Field(None, description="Human-readable status details")
    metrics: Dict[str, Any] = Field(default_factory=dict, description="Quantitative metrics")


class ErrorPayload(BaseModel):
    """Error notification payload for ERROR messages"""

    payload_type: Literal["error"] = Field("error", description="Payload discriminator")
    phase: str = Field(..., description="Phase where error occurred")
    error_type: str = Field(..., description="Exception type")
    message: str = Field(..., description="Error message")
    traceback: Optional[str] = Field(None, description="Formatted traceback string")
    artifact_path: Optional[str] = Field(None, description="Path to persisted error artifact")
    rescue_suggestion: Optional[str] = Field(None, description="Suggested remediation step")


class QueryPayload(BaseModel):
    """Query payload for QUERY messages"""

    payload_type: Literal["query"] = Field("query", description="Payload discriminator")
    query: str = Field(..., description="Query or request content")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Query parameters or filters")
    expected_response: Optional[str] = Field(None, description="Expected response description")


class AgentMessage(BaseModel):
    """
    Complete agent message with header and typed payload
    
    Implements structured communication protocol for AI University workflow.
    Messages are self-contained with full context for reproducibility.
    """
    header: MessageHeader = Field(..., description="Message routing and metadata")
    intent: ResearchIntent = Field(..., description="Research goal and hypothesis")
    payload: Union[
        ProposalPayload,
        ExperimentPayload,
        ResultPayload,
        ValidationPayload,
        StatusPayload,
        ErrorPayload,
        QueryPayload,
    ] = Field(
        ..., 
        description="Message-specific payload",
        discriminator="payload_type"
    )
    attachments: List[Attachment] = Field(default_factory=list, description="Supporting files")
    artifact_refs: List[str] = Field(default_factory=list, description="Artifact identifiers or paths")
    evidence_refs: List[str] = Field(default_factory=list, description="Evidence ledger or claim references")
    source_ids: List[str] = Field(default_factory=list, description="Canonical external source identifiers")
    context: Dict[str, Any] = Field(default_factory=dict, description="Additional context")
    
    @model_validator(mode='after')
    def validate_payload_type_consistency(self):
        """Ensure payload type matches message type"""
        type_mapping = {
            MessageType.PROPOSAL: ProposalPayload,
            MessageType.EXPERIMENT: ExperimentPayload,
            MessageType.RESULT: ResultPayload,
            MessageType.VALIDATION: ValidationPayload,
            MessageType.STATUS: StatusPayload,
            MessageType.ERROR: ErrorPayload,
            MessageType.QUERY: QueryPayload,
        }
        
        expected_type = type_mapping.get(self.header.message_type)
        if expected_type and not isinstance(self.payload, expected_type):
            raise ValueError(
                f"Message type {self.header.message_type} expects payload type {expected_type.__name__}"
            )
        return self
    
    def to_bitacora_entry(self) -> str:
        """
        Convert message to Bitácora log entry format
        
        Follows DAILY_LOGS format from AI University workflow
        """
        lines = [
            f"## {self.header.message_type.value.upper()} – {self.header.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**From**: {self.header.sender_persona.value}",
            f"**Phase**: {self.header.roadmap_phase}",
            f"**Goal**: {self.intent.goal}",
            ""
        ]
        
        if self.intent.hypothesis:
            lines.append(f"**Hypothesis**: {self.intent.hypothesis}")
            lines.append("")
        
        lines.append("**Payload**:")
        if isinstance(self.payload, BaseModel):
            for field, value in self.payload.model_dump().items():
                lines.append(f"- {field}: {value}")
        
        if self.attachments:
            lines.append("")
            lines.append("**Attachments**:")
            for att in self.attachments:
                lines.append(f"- [{att.description}]({att.file_path})")
        
        return "\n".join(lines)
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "header": {
                    "sender_persona": "dr_yuan_chen",
                    "message_type": "experiment",
                    "roadmap_phase": "4.002",
                },
                "intent": {
                    "goal": "Extract ESE features from mdCATH trajectories",
                    "hypothesis": "ESE captures conformational dynamics better than static descriptors",
                    "required_evidence": ["ESE signatures", "Validation metrics"],
                    "success_criteria": ["Coverage >90%", "Dimensionality 512"],
                },
                "payload": {
                    "experiment_name": "mdCATH_ESE_Extraction_Batch_001",
                    "protocol": "Apply Chronosfold ESE extractor to mdCATH H5 files",
                    "parameters": {"input_dir": "/data/mdcath", "output_dim": 512},
                    "expected_duration": "4 hours",
                    "validation_checks": ["Check dimension consistency", "Validate no NaN values"],
                },
                "attachments": [],
                "context": {"batch_size": 100},
            }
        }
    )
