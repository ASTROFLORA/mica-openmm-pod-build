from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class UnifiedTrustState(str, Enum):
    SOURCE_CANONICAL = "source_canonical"
    RESOLVED_IDENTITY = "resolved_identity"
    PROMOTED_CLAIM = "promoted_claim"
    GRAPH_RECEIPTED = "graph_receipted"
    EXECUTION_OBSERVED = "execution_observed"
    STATIC_DERIVED = "static_derived"
    PROJECTION = "projection"
    RETRIEVAL_CANDIDATE = "retrieval_candidate"
    LOCAL_UNCOMMITTED = "local_uncommitted"
    DEGRADED = "degraded"
    MISSING = "missing"
    CONFLICTING = "conflicting"

class UnifiedVisibility(str, Enum):
    SANDBOX_ONLY = "sandbox_only"
    TENANT_PRIVATE = "tenant_private"
    CONSORTIUM_SHARED = "consortium_shared"
    MICA_GLOBAL = "mica_global"
    EXTERNAL_PUBLIC = "external_public"

class UnifiedTypedBlocker(BaseModel):
    blocker_code: str
    classification: str  # e.g., broken, fail_closed_by_design, provider_unavailable, etc.
    human_message: str
    safe_fallback: str = "none"
    production_ready: bool = False

class UnifiedCodeIdentity(BaseModel):
    code_ref: str
    code_sha256: str
    repo_commit_sha: Optional[str] = None
    repo_diff_sha256: Optional[str] = None
    lockfile_sha256: Optional[str] = None
    image_digest: Optional[str] = None

class UnifiedTenancyContext(BaseModel):
    tenant_ref: str
    actor_ref: str
    owner_user_id: Optional[str] = None
    scope_ref: str
    permission_scope_ref: str
    scope_kind: str
    visibility_ceiling: UnifiedVisibility
    provider_policy_ref: Optional[str] = None
    data_residency_region: Optional[str] = None
    can_read: bool = False
    can_write: bool = False
    can_create_artifact: bool = False
    can_run_sandbox: bool = False
    can_mount_secret: bool = False
    can_deploy_model: bool = False
    can_export: bool = False
    can_promote_visibility: bool = False
    source_authority: str

class UnifiedSecretMountReceipt(BaseModel):
    receipt_ref: str
    secret_mount_ref: str
    decision: str  # approved, rejected, expired, revoked
    reason_codes: List[str] = Field(default_factory=list)
    mounted_value_logged: bool = False
    audit_ref: str

class UnifiedReceiptCore(BaseModel):
    receipt_ref: str
    receipt_version: str = "v1"
    kind: str  # command, protocol_node, sandbox, serverless, etc.
    execution_status: str  # ok, submitted, completed, blocked, failed, partial
    policy_decision: Optional[str] = None  # allow, reject, approved, etc.
    workspace_ref: Optional[str] = None
    tenant_ref: Optional[str] = None
    actor_ref: Optional[str] = None
    operation_name: str
    input_refs: List[str] = Field(default_factory=list)
    output_refs: List[str] = Field(default_factory=list)
    parent_receipt_refs: List[str] = Field(default_factory=list)
    policy_refs: List[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    trace_ref: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
    actual_cost_usd: Optional[float] = None
    code_identity: Optional[UnifiedCodeIdentity] = None
