from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

# Enums
class BlockKind(str, Enum):
    TASK = "task"
    RED_LINES = "red_lines"
    TOOL_PERMISSIONS = "tool_permissions"
    DOCTRINE = "doctrine"
    MEMORY_DIGEST = "memory_digest"
    EVIDENCE_REQS = "evidence_reqs"
    RETRIEVED_CONTEXT = "retrieved_context"
    OUTPUT_CONTRACT = "output_contract"
    UNCERTAINTY = "uncertainty"
    USER_INPUT = "user_input"

class SourceAuthority(str, Enum):
    DOCTRINE_REGISTRY = "DOCTRINE_REGISTRY"
    TENANCY_PDP = "TENANCY_PDP"
    CEA = "CEA"
    MUDO_MEMORY = "MUDO_MEMORY"
    COMMAND_KERNEL = "COMMAND_KERNEL"
    CS_COMPOSITION = "CS_COMPOSITION"
    USER_INPUT = "USER_INPUT"
    UNKNOWN = "UNKNOWN"

class TrustState(str, Enum):
    POLICY_SCOPED = "policy_scoped"
    DOCTRINE_VERSIONED = "doctrine_versioned"
    MEMORY_DIGEST = "memory_digest"
    RETRIEVAL_CANDIDATE = "retrieval_candidate"
    TOOL_DECLARED = "tool_declared"
    USER_SUPPLIED = "user_supplied"
    DEGRADED = "degraded"
    MISSING = "missing"

class PositionClass(str, Enum):
    ANCHOR_HEAD = "anchor_head"
    MIDDLE_FILL = "middle_fill"
    ANCHOR_TAIL = "anchor_tail"

class OverflowPolicy(str, Enum):
    DROP_LOWEST_RANK = "drop_lowest_rank"
    SUMMARIZE_MIDDLE = "summarize_middle"
    REJECT = "reject"

class CommitLevel(str, Enum):
    MUDO_COMMITTED = "mudo_committed"
    CS_LOCAL_TRACE_ONLY = "cs_local_trace_only"

class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_WITH_OBLIGATIONS = "allow_with_obligations"

class Obligation(str, Enum):
    WATERMARK = "watermark"
    NO_CACHE = "no_cache"
    AUDIT_REQUIRED = "audit_required"
    REDACT_FIELDS = "redact_fields"
    HITL_REQUIRED = "hitl_required"
    REGION_LOCK = "region_lock"
    RETENTION_LIMIT = "retention_limit"

class Channel(str, Enum):
    TRUSTED_INSTRUCTION = "trusted_instruction"
    UNTRUSTED_USER_TEXT = "untrusted_user_text"
    RETRIEVED_TEXT = "retrieved_text"
    TOOL_OUTPUT = "tool_output"

class PostCheckKind(str, Enum):
    POLICY_VIOLATION = "policy_violation"
    EVIDENCE_PRESENCE = "evidence_presence"
    PROMPT_LEAKAGE = "prompt_leakage"
    TOOL_MISUSE = "tool_misuse"
    UNCERTAINTY_MISSING = "uncertainty_missing"
    SYNTHETIC_LABEL_MISSING = "synthetic_label_missing"
    PDP_OBLIGATION = "pdp_obligation"
    CHANNEL_VIOLATION = "channel_violation"

class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    HITL = "hitl"

class OnFailAction(str, Enum):
    REJECT_OUTPUT = "reject_output"
    REDACT_AND_WARN = "redact_and_warn"
    ESCALATE_HITL = "escalate_hitl"

class DoctrineStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"
    UNDER_APPEAL = "under_appeal"

# Models
class ContextBlock(BaseModel):
    block_id: str
    block_kind: BlockKind
    source_authority: SourceAuthority
    trust_state: TrustState
    refs: List[str] = Field(default_factory=list)
    content: str
    content_hash: str
    token_estimate: int
    position_class: PositionClass
    rank_score: float

class ContextEnvelope(BaseModel):
    context_envelope_ref: str
    schema_version: str = "urn:mica:cs:ContextEnvelope:W0:v1"
    actor_ref: str
    session_ref: str
    intent_class: str
    resource_class: str
    permission_decision_ref: str
    doctrine_version: str
    policy_version: str
    blocks: List[ContextBlock]
    context_budget_tokens: int
    tokens_used: int
    overflow_policy: OverflowPolicy = OverflowPolicy.DROP_LOWEST_RANK
    context_hash: str
    prompt_assembly_receipt_ref: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class PromptAssemblyReceipt(BaseModel):
    prompt_assembly_receipt_ref: str
    schema_version: str = "urn:mica:cs:PromptAssemblyReceipt:W0:v1"
    context_envelope_ref: str
    context_hash: str
    input_message_hash: str
    doctrine_refs: List[str] = Field(default_factory=list)
    policy_decision_ref: str
    memory_refs: List[str] = Field(default_factory=list)
    tool_permissions_ref: str
    block_hashes: List[str] = Field(default_factory=list)
    assembled_prompt_hash: str
    prev_receipt_hash: str
    this_receipt_hash: str
    commit_level: CommitLevel
    written_by: str = "provenance_writer"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class PermissionDecisionReceipt(BaseModel):
    permission_decision_ref: str
    decision: Decision
    policy_bundle_ref: str
    obligations: List[Obligation] = Field(default_factory=list)
    effective_ttl: int
    decided_at: datetime = Field(default_factory=datetime.utcnow)

class PostCheck(BaseModel):
    check_id: str
    check_kind: PostCheckKind
    severity: Severity
    on_fail: OnFailAction
    obligation_source: Optional[str] = None

class DoctrineRef(BaseModel):
    doctrine_ref: str
    domain: str
    version: int
    scope: str
    status: DoctrineStatus
    effective_from: datetime
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    owner_role: str = "doctrine_owner"
    content: str
    content_hash: str
    immutable_by_version: bool = True
    appealable: bool = True

class DoctrineAppeal(BaseModel):
    appeal_ref: str
    doctrine_ref: str
    raised_by: str
    rationale: str
    status: str = "open"
    resolution_doctrine_ref: Optional[str] = None
    decided_by: str = "doctrine_owner"

class CSClosureSignature(BaseModel):
    signer_ref: str
    signer_role: str
    decision: str

class CSClosureReceipt(BaseModel):
    receipt_ref: str
    lane_ref: str = "CS"
    closure_level: str = "CS-W0"
    schema_version: str = "urn:mica:cs:CSClosureReceipt:W0:v1"
    context_envelope_contract_passed: bool
    prompt_assembly_receipt_passed: bool
    pdp_integration_before_assembly_passed: bool
    postcheck_enforcement_passed: bool
    doctrine_registry_passed: bool
    training_firewall_passed: bool
    no_hidden_sovereign_tests_passed: bool
    degraded_mode_passed: bool
    sample_envelope_refs: List[str] = Field(default_factory=list)
    test_report_ref: str
    signatures: List[CSClosureSignature] = Field(default_factory=list)
    closure_decision: str
